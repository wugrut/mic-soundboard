"""Automatic Windows 11 default microphone switcher & restorer via Core Audio IPolicyConfig."""

from __future__ import annotations

import atexit
import ctypes
import json
import logging
from ctypes import c_int, c_wchar_p
from pathlib import Path
from typing import Optional

logger = logging.getLogger("mic_soundboard.win_mic")

try:
    import comtypes
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
    from pycaw.pycaw import AudioUtilities, EDataFlow, ERole

    CLSID_PolicyConfigClient = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")

    class IPolicyConfig(IUnknown):
        _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetMixFormat", (["in"], c_wchar_p, "pwstrDeviceId")),
            COMMETHOD([], HRESULT, "GetDeviceFormat", (["in"], c_wchar_p, "pwstrDeviceId")),
            COMMETHOD([], HRESULT, "ResetDeviceFormat", (["in"], c_wchar_p, "pwstrDeviceId")),
            COMMETHOD([], HRESULT, "SetDeviceFormat", (["in"], c_wchar_p, "pwstrDeviceId")),
            COMMETHOD([], HRESULT, "GetProcessingPeriod", (["in"], c_wchar_p, "pwstrDeviceId")),
            COMMETHOD([], HRESULT, "SetProcessingPeriod", (["in"], c_wchar_p, "pwstrDeviceId")),
            COMMETHOD([], HRESULT, "GetShareMode", (["in"], c_wchar_p, "pwstrDeviceId")),
            COMMETHOD([], HRESULT, "SetShareMode", (["in"], c_wchar_p, "pwstrDeviceId")),
            COMMETHOD([], HRESULT, "GetPropertyValue", (["in"], c_wchar_p, "pwstrDeviceId")),
            COMMETHOD([], HRESULT, "SetPropertyValue", (["in"], c_wchar_p, "pwstrDeviceId")),
            COMMETHOD([], HRESULT, "SetDefaultEndpoint", (["in"], c_wchar_p, "pwstrDeviceId"), (["in"], c_int, "role")),
            COMMETHOD([], HRESULT, "SetEndpointVisibility", (["in"], c_wchar_p, "pwstrDeviceId"), (["in"], c_int, "bVisible")),
        ]

    _COM_AVAILABLE = True
except Exception:
    _COM_AVAILABLE = False


class DefaultMicSwitcher:
    """Switches Windows Default Recording Device to the virtual mic while active, and restores on exit."""

    def __init__(self, backup_path: Path):
        self.backup_path = backup_path
        self.active = False
        self.switched_to_name: Optional[str] = None
        self._original_id: Optional[str] = None
        self._ctrl_handler_ref = None

    def _set_default_capture_id(self, endpoint_id: str) -> None:
        if not _COM_AVAILABLE:
            return
        comtypes.CoInitialize()
        pc = comtypes.CoCreateInstance(CLSID_PolicyConfigClient, interface=IPolicyConfig)
        for role in (0, 1, 2):  # eConsole, eMultimedia, eCommunications
            pc.SetDefaultEndpoint(endpoint_id, role)

    def _find_capture_id_by_name(self, name: str) -> Optional[str]:
        if not _COM_AVAILABLE:
            return None
        comtypes.CoInitialize()
        target_low = name.lower()
        for d in AudioUtilities.GetAllDevices():
            if d and d.id and "{0.0.1." in d.id and d.FriendlyName:
                if d.FriendlyName.lower() == target_low or target_low in d.FriendlyName.lower():
                    return d.id
        return None

    def _get_current_default_id(self) -> Optional[str]:
        if not _COM_AVAILABLE:
            return None
        comtypes.CoInitialize()
        enum = AudioUtilities.GetDeviceEnumerator()
        dev = enum.GetDefaultAudioEndpoint(EDataFlow.eCapture.value, ERole.eConsole.value)
        return dev.GetId()

    def activate(self, target_mic_name: str) -> bool:
        if not _COM_AVAILABLE:
            return False
        try:
            target_id = self._find_capture_id_by_name(target_mic_name)
            if not target_id:
                logger.warning("Could not find Windows capture endpoint for %s", target_mic_name)
                return False

            current_id = self._get_current_default_id()
            if current_id and current_id != target_id:
                self._original_id = current_id
                self.backup_path.parent.mkdir(parents=True, exist_ok=True)
                self.backup_path.write_text(json.dumps({"original_mic_id": current_id}), encoding="utf-8")
            elif self.backup_path.exists():
                try:
                    data = json.loads(self.backup_path.read_text(encoding="utf-8"))
                    self._original_id = data.get("original_mic_id")
                except Exception:
                    pass

            if current_id != target_id:
                self._set_default_capture_id(target_id)
                logger.info("Switched Windows default mic -> %s", target_mic_name)

            self.active = True
            self.switched_to_name = target_mic_name
            self._install_exit_hooks()
            return True
        except Exception as exc:
            logger.warning("Failed to switch Windows default mic: %s", exc)
            return False

    def restore(self) -> bool:
        if not _COM_AVAILABLE:
            return False
        orig_id = self._original_id
        if not orig_id and self.backup_path.exists():
            try:
                orig_id = json.loads(self.backup_path.read_text(encoding="utf-8")).get("original_mic_id")
            except Exception:
                orig_id = None

        if not orig_id:
            self.active = False
            return False

        try:
            self._set_default_capture_id(orig_id)
            self.active = False
            self.switched_to_name = None
            if self.backup_path.exists():
                self.backup_path.unlink(missing_ok=True)
            logger.info("Restored original Windows default mic (%s)", orig_id)
            return True
        except Exception as exc:
            logger.warning("Failed to restore Windows default mic: %s", exc)
            return False

    def _install_exit_hooks(self) -> None:
        atexit.register(self.restore)
        if self._ctrl_handler_ref is not None:
            return
        try:
            HandlerRoutine = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

            def _on_ctrl(ctrl_type: int) -> bool:
                # 0=CTRL_C, 1=CTRL_BREAK, 2=CTRL_CLOSE, 5=LOGOFF, 6=SHUTDOWN
                if ctrl_type in (0, 1, 2, 5, 6):
                    self.restore()
                return False

            self._ctrl_handler_ref = HandlerRoutine(_on_ctrl)
            ctypes.windll.kernel32.SetConsoleCtrlHandler(self._ctrl_handler_ref, True)
        except Exception:
            pass
