import contextlib
import json
import time
from copy import deepcopy  # <-- added
from subprocess import Popen, TimeoutExpired
from threading import Thread
from typing import Callable, Optional

from wyzebridge.wyze_api import WyzeApi
from wyzebridge.stream import Stream
from wyzebridge.config import MOTION, MQTT_DISCOVERY, SNAPSHOT_TYPE
from wyzebridge.ffmpeg import rtsp_snap_cmd, wait_for_purges
from wyzebridge.logging import logger
from wyzebridge.mqtt import bridge_status, cam_control, publish_topic, update_preview
from wyzebridge.mtx_event import RtspEvent
from wyzebridge.wyze_events import WyzeEvents
from wyzebridge.bridge_utils_sunset import should_take_snapshot, should_skip_snapshot


# -------- DUO helper (module-level) -----------------------------------------
def _duo_variants(stream: Stream) -> list[Stream]:
    """
    If the incoming Stream is a Duo model, return two cloned streams:
      - channel 0 (PTZ)  -> uri suffix '-ptz'
      - channel 1 (Wide) -> uri suffix '-wide'
    Otherwise, return [stream] unchanged.
    """
    # Try to read model from stream.cam.product_model (preferred),
    # fall back to stream.product_model if present.
    cam = getattr(stream, "cam", None)
    product_model = (
        getattr(cam, "product_model", None)
        or getattr(stream, "product_model", None)
        or ""
    )

    # Support both names we’ve seen used in the wild
    if product_model not in {"WL_DUO", "GW_DUO"}:
        return [stream]

    def _mk_variant(src: Stream, channel: int, suffix: str, title_suffix: str) -> Stream:
        s = deepcopy(src)

        # channel flag (used by lower layers to pick the second feed)
        try:
            setattr(s, "channel", channel)
        except Exception:
            pass

        # make a unique, stable uri
        base_uri = getattr(src, "uri", None) or getattr(src, "name_uri", None)
        if not base_uri:
            base_uri = f"{getattr(cam, 'name_uri', 'cam')}"
        s.uri = f"{base_uri}-{suffix}"

        # pretty name if UI uses it
        # prefer s.cam.nickname if available
        nick = None
        try:
            if hasattr(s, "cam") and getattr(s.cam, "nickname", None):
                nick = s.cam.nickname
        except Exception:
            pass
        nick = nick or getattr(src, "uri", "Camera")
        if hasattr(s, "display_name"):
            s.display_name = f"{nick} {title_suffix}"
        elif hasattr(s, "name"):
            s.name = f"{nick} {title_suffix}"
        # don’t touch s.cam.nickname (keep the physical device name)

        return s

    ptz = _mk_variant(stream, 0, "ptz", "(PTZ)")
    wide = _mk_variant(stream, 1, "wide", "(Wide)")
    logger.info(f"[STREAM] Expanding DUO model into two streams: {ptz.uri}, {wide.uri}")
    return [ptz, wide]
# ---------------------------------------------------------------------------


class StreamManager:
    __slots__ = "api", "stop_flag", "streams", "rtsp_snapshots", "last_snap", "monitor_snapshots_thread"

    def __init__(self, api: WyzeApi):
        self.api: WyzeApi = api
        self.stop_flag: bool = False
        self.streams: dict[str, Stream] = {}
        self.rtsp_snapshots: dict[str, Popen] = {}
        self.last_snap: float = 0
        self.monitor_snapshots_thread: Optional[Thread] = None

    @property
    def total(self):
        return len(self.streams)

    @property
    def active(self):
        return len([s for s in self.streams.values() if s.enabled])

    def add(self, stream: Stream) -> str:
        """
        Register a Stream. If the camera is a DUO, this will register
        two logical streams (PTZ and Wide) and return the PTZ uri.
        """
        variants = _duo_variants(stream)
        first_uri = None
        for v in variants:
            uri = v.uri
            if first_uri is None:
                first_uri = uri
            self.streams[uri] = v
        return first_uri or stream.uri

    def get(self, uri: str) -> Optional[Stream]:
        return self.streams.get(uri)

    def get_info(self, uri: str) -> dict:
        return stream.get_info() if (stream := self.get(uri)) else {}

    def get_all_cam_info(self) -> dict:
        return {uri: s.get_info() for uri, s in self.streams.items()}

    def stop_all(self) -> None:
        logger.info(f"[STREAM] Stopping {self.total} stream{'s'[:self.total^1]}")
        self.stop_flag = True

        for stream in self.streams.values():
            stream.stop()

        if self.monitor_snapshots_thread is not None:
            logger.info("[STREAM] Stopping monitor_snapshots thread")
            with contextlib.suppress(ValueError, AttributeError, RuntimeError):
                self.monitor_snapshots_thread.join(timeout=5)
            self.monitor_snapshots_thread = None

        wait_for_purges()

    def monitor_streams(self, mtx_health: Callable) -> None:
        self.stop_flag = False

        if MQTT_DISCOVERY:
            self.monitor_snapshots()

        mqtt = cam_control(self.streams, self.send_cmd)
        logger.info(f"🎬 {self.total} stream{'s'[:self.total^1]} enabled")
        event = RtspEvent(self.streams)
        events = WyzeEvents(self.streams) if MOTION else None

        while not self.stop_flag:
            event.read(timeout=1)
            self.snap_all(self.active_streams())

            if events:
                events.check_motion()

            if int(time.time()) % 15 == 0:
                mtx_health()
                bridge_status(mqtt)

        if mqtt:
            logger.info("[STREAM] Stopping mqtt loop")
            mqtt.loop_stop()
            mqtt = None

        logger.info("[STREAM] Stream monitoring stopped")

    def monitor_snapshots(self) -> None:
        def wrapped():
            logger.info("[STREAM] Starting monitor_snapshots thread")
            try:
                # emit to MQTT the current snapshots on file system
                for cam in self.streams:
                    if not self.stop_flag:
                        update_preview(cam)

                while not self.stop_flag:
                    for cam, ffmpeg in list(self.rtsp_snapshots.items()):
                        if not self.stop_flag and ffmpeg is not None and (returncode := ffmpeg.returncode) is not None:
                            if returncode == 0:
                                update_preview(cam)
                            # we have some response, remove from queue
                            self.remove_from_rtsp_snapshots(cam)
                    time.sleep(1)
            except Exception as e:
                logger.error(f"[STREAM] Unexpected error in monitor_snapshots: {e}")

        if self.monitor_snapshots_thread is not None:
            logger.info("[STREAM] Stopping previous monitor_snapshots thread")
            with contextlib.suppress(ValueError, AttributeError, RuntimeError):
                self.monitor_snapshots_thread.join(timeout=5)
            self.monitor_snapshots_thread = None

        self.monitor_snapshots_thread = Thread(target=wrapped, name="monitor_snapshots")
        self.monitor_snapshots_thread.daemon = True  # allow this thread to be abandoned
        self.monitor_snapshots_thread.start()

    def remove_from_rtsp_snapshots(self, cam: str):
        try:
            del self.rtsp_snapshots[cam]
        except KeyError:
            logger.warning(f"[STREAM] {cam} not found in rtsp snapshots.")
        except Exception as ex:
            logger.error(f"[STREAM] [{type(ex).__name__}] removing {cam=} {ex}.")

    def active_streams(self) -> list[str]:
        """
        Health check on all streams and return a list of enabled
        streams that are NOT battery powered.

        Returns:
        - list(str): uri-friendly name of streams that are enabled.
        """
        if self.stop_flag:
            return []
        return [cam for cam, s in self.streams.items() if s.health_check() > 0]

    def snap_all(self, cams: Optional[list[str]] = None, force: bool = False):
        """
        Take an rtsp snapshot of the streams in the list.

        Args:
        - cams (list[str], optional): names of the streams to take a snapshot of.
        - force (bool, optional): Ignore interval and force snapshot. Defaults to False.
        """
        if force or should_take_snapshot(SNAPSHOT_TYPE, self.last_snap):
            self.last_snap = time.time()
            for cam_name in cams or self.active_streams():
                if should_skip_snapshot(cam_name):
                    continue
                if SNAPSHOT_TYPE == "rtsp":
                    self.stop_subprocess(cam_name)
                    self.rtsp_snap_popen(cam_name, True)
                elif SNAPSHOT_TYPE == "api":
                    self.api.save_thumbnail(cam_name, "")

    def get_sse_status(self) -> dict:
        return {
            uri: {"status": cam.status(), "motion": cam.motion}
            for uri, cam in self.streams.items()
        }

    def send_cmd(
        self, cam_name: str, cmd: str, payload: str | list | dict = ""
    ) -> dict:
        """
        Send a command directly to the camera and wait for a response.

        Parameters:
        - cam_name (str): uri-friendly name of the camera.
        - cmd (str): The camera/tutk command to send.
        - payload (str): value for the tutk command.

        Returns:
        - dictionary: Results that can be converted to JSON.
        """
        resp = {"status": "error", "command": cmd, "payload": payload}

        if cam_name == "all" and cmd == "update_snapshot":
            self.snap_all(force=True)
            return resp | {"status": "success"}

        if not (stream := self.get(cam_name)):
            return resp | {"response": "Camera not found"}

        if cam_resp := stream.send_cmd(cmd, payload):
            status = cam_resp.get("value") if cam_resp.get("status") == "success" else 0

            if isinstance(status, dict):
                status = json.dumps(status)

            if "update_snapshot" in cam_resp:
                demand_opened = not stream.connected
                snap = self.get_rtsp_snap(cam_name)
                if demand_opened:
                    stream.stop()

                publish_topic(f"{cam_name}/{cmd}", int(time.time()) if snap else 0)
                return dict(resp, status="success", value=snap, response=snap)

            publish_topic(f"{cam_name}/{cmd}", status)

        return cam_resp if "status" in cam_resp else resp | cam_resp

    def rtsp_snap_popen(self, cam_name: str, interval: bool = False) -> Optional[Popen]:
        if not (stream := self.get(cam_name)):
            return
        stream.start()
        ffmpeg = self.rtsp_snapshots.get(cam_name)
        if not ffmpeg or ffmpeg.poll() is not None:
            # None means inherit from parent process
            ffmpeg = Popen(rtsp_snap_cmd(cam_name, interval), stderr=None)
            self.rtsp_snapshots[cam_name] = ffmpeg
        return ffmpeg

    def get_rtsp_snap(self, cam_name: str) -> bool:
        if not (stream := self.get(cam_name)) or stream.health_check() < 1:
            return False
        if not (ffmpeg := self.rtsp_snap_popen(cam_name)):
            return False
        try:
            if ffmpeg.wait(timeout=15) == 0:
                return True
        except TimeoutExpired:
            logger.info(f"❗ [{cam_name}] Snapshot timed out")
        except Exception as ex:
            logger.error(f"❗ [{cam_name}] [{type(ex).__name__}] {ex}")
        finally:
            self.stop_subprocess(cam_name)
        return False

    def stop_subprocess(self, cam: str):
        ffmpeg = self.rtsp_snapshots.get(cam)

        if ffmpeg is not None:
            self.remove_from_rtsp_snapshots(cam)

            if ffmpeg.poll() is None:
                ffmpeg.kill()
                ffmpeg.communicate()
