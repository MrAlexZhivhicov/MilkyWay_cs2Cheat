"""Milky Way — compact external CS2 utility.

The application intentionally contains no anti-cheat bypass or process-hiding code.
Use only where game/server rules allow it.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import math
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import keyboard
import pymem
import pymem.process
import requests
import tkinter as tk
from tkinter import colorchooser, messagebox, ttk

APP_NAME = "Milky Way"
APP_VERSION = "2.0"
PROCESS_NAME = "cs2.exe"
OFFSETS_URL = "https://raw.githubusercontent.com/a2x/cs2-dumper/main/output/offsets.json"
CLIENT_URL = "https://raw.githubusercontent.com/a2x/cs2-dumper/main/output/client_dll.json"
CONFIG_PATH = Path(os.getenv("LOCALAPPDATA", Path.home())) / "MilkyWay" / "settings.json"
LOG_PATH = CONFIG_PATH.with_name("milky_way.log")

WEAPON_NAMES_BY_ID = {
    1: "Desert Eagle", 2: "Dual Berettas", 3: "Five-SeveN", 4: "Glock-18",
    7: "AK-47", 8: "AUG", 9: "AWP", 10: "FAMAS", 11: "G3SG1", 13: "Galil AR",
    16: "M4A4", 17: "MAC-10", 19: "P90", 24: "UMP-45", 30: "Tec-9",
    33: "MP7", 34: "MP9", 36: "P250", 38: "SCAR-20", 39: "SG 553",
    40: "SSG 08", 60: "M4A1-S", 61: "USP-S", 64: "R8 Revolver",
}


def configure_logging() -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        encoding="utf-8",
    )


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    glow: bool = True
    anti_flash: bool = True
    bunny_hop: bool = False
    no_recoil: bool = False
    recoil_strength: float = 100.0
    no_shake: bool = True
    aim_enabled: bool = False
    aim_smooth: float = 8.0
    aim_fov: float = 6.0
    aim_target: str = "head"
    show_fov: bool = True
    box_esp: bool = False
    esp_name: bool = True
    esp_health: bool = True
    esp_weapon: bool = True
    esp_armor: bool = True
    esp_distance: bool = True
    esp_snapline: bool = False
    esp_head_dot: bool = False
    box_color: str = "#9b5cff"
    name_color: str = "#f4f4f7"
    hp_color: str = "#55dd77"
    armor_color: str = "#58a6ff"
    weapon_color: str = "#c8cad3"
    fov_color: str = "#9b5cff"
    crosshair_enabled: bool = False
    crosshair_color: str = "#ffffff"
    crosshair_size: float = 6.0
    watermark: bool = True
    overlay_fps: bool = False
    esp_fill: bool = False
    radar_hack: bool = False
    health_color: bool = True
    custom_color: str = "#00e5ff"

    @classmethod
    def load(cls) -> "Settings":
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            allowed = {key: raw[key] for key in cls.__annotations__ if key in raw}
            result = cls(**allowed)
            if not valid_hex_color(result.custom_color):
                return replace(result, custom_color=cls.custom_color)
            return result
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")


def valid_hex_color(value: str) -> bool:
    if len(value) != 7 or not value.startswith("#"):
        return False
    try:
        int(value[1:], 16)
        return True
    except ValueError:
        return False


class StateStore:
    """Thread-safe settings snapshot for worker threads."""

    def __init__(self, settings: Settings):
        self._value = settings
        self._lock = threading.Lock()

    def get(self) -> Settings:
        with self._lock:
            return self._value

    def set(self, **changes: object) -> Settings:
        with self._lock:
            self._value = replace(self._value, **changes)
            return self._value


class OffsetError(RuntimeError):
    pass


def nested_int(data: dict, *path: str) -> int:
    value: object = data
    try:
        for key in path:
            value = value[key]  # type: ignore[index]
        result = int(value)  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError) as exc:
        raise OffsetError(f"Не найдено поле: {' → '.join(path)}") from exc
    if result < 0:
        raise OffsetError(f"Некорректное смещение: {' → '.join(path)}")
    return result


def download_json(url: str) -> dict:
    response = requests.get(url, timeout=(4, 12), headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise OffsetError("Сервер вернул данные неизвестного формата")
    return payload


class Cheats:
    def __init__(self, pm: pymem.Pymem, client: int, state: StateStore, stop: threading.Event):
        self.pm = pm
        self.client = client
        self.state = state
        self.stop = stop
        # Both independent dumps are fetched concurrently to avoid a frozen-looking startup.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="offsets") as pool:
            offsets_job = pool.submit(download_json, OFFSETS_URL)
            client_job = pool.submit(download_json, CLIENT_URL)
            offsets = offsets_job.result()
            client_dll = client_job.result()

        self.dw_entity_list = nested_int(offsets, "client.dll", "dwEntityList")
        self.dw_local_player = nested_int(offsets, "client.dll", "dwLocalPlayerPawn")
        self.dw_view_angles = nested_int(offsets, "client.dll", "dwViewAngles")
        self.dw_view_matrix = nested_int(offsets, "client.dll", "dwViewMatrix")
        classes = client_dll["client.dll"]["classes"]
        self.team = nested_int(classes, "C_BaseEntity", "fields", "m_iTeamNum")
        self.player_pawn = nested_int(classes, "CCSPlayerController", "fields", "m_hPlayerPawn")
        self.flash_duration = nested_int(classes, "C_CSPlayerPawnBase", "fields", "m_flFlashDuration")
        self.life_state = nested_int(classes, "C_BaseEntity", "fields", "m_lifeState")
        self.health = nested_int(classes, "C_BaseEntity", "fields", "m_iHealth")
        self.flags = nested_int(classes, "C_BaseEntity", "fields", "m_fFlags")
        self.shots_fired = nested_int(classes, "C_CSPlayerPawn", "fields", "m_iShotsFired")
        # Aim-punch data was moved into a dedicated service in recent CS2 builds.
        self.aim_punch_services = nested_int(classes, "C_CSPlayerPawn", "fields", "m_pAimPunchServices")
        self.predictable_punch = nested_int(classes, "CCSPlayer_AimPunchServices", "fields", "m_predictableBaseAngle")
        self.unpredictable_punch = nested_int(classes, "CCSPlayer_AimPunchServices", "fields", "m_unpredictableBaseAngle")
        self.camera_services = nested_int(classes, "C_BasePlayerPawn", "fields", "m_pCameraServices")
        self.view_punch = nested_int(classes, "CPlayer_CameraServices", "fields", "m_vecCsViewPunchAngle")
        self.scene_node = nested_int(classes, "C_BaseEntity", "fields", "m_pGameSceneNode")
        self.abs_origin = nested_int(classes, "CGameSceneNode", "fields", "m_vecAbsOrigin")
        self.armor = nested_int(classes, "C_CSPlayerPawn", "fields", "m_ArmorValue")
        self.player_name = nested_int(classes, "CCSPlayerController", "fields", "m_sSanitizedPlayerName")
        self.spotted_state = nested_int(classes, "C_CSPlayerPawn", "fields", "m_entitySpottedState")
        self.spotted = nested_int(classes, "EntitySpottedState_t", "fields", "m_bSpotted")
        self.weapon_services = nested_int(classes, "C_BasePlayerPawn", "fields", "m_pWeaponServices")
        self.active_weapon = nested_int(classes, "CPlayer_WeaponServices", "fields", "m_hActiveWeapon")
        self.attribute_manager = nested_int(classes, "C_EconEntity", "fields", "m_AttributeManager")
        self.econ_item = nested_int(classes, "C_AttributeContainer", "fields", "m_Item")
        self.item_definition = nested_int(classes, "C_EconItemView", "fields", "m_iItemDefinitionIndex")
        self.glow = nested_int(classes, "C_BaseModelEntity", "fields", "m_Glow")
        self.glowing = nested_int(classes, "CGlowProperty", "fields", "m_bGlowing")
        self.glow_color = nested_int(classes, "CGlowProperty", "fields", "m_glowColorOverride")
        self.glow_type = nested_int(classes, "CGlowProperty", "fields", "m_iGlowType")

    def _pause(self, seconds: float) -> bool:
        return self.stop.wait(seconds)

    def _pawns(self) -> list[int]:
        return [pawn for _controller, pawn in self._player_records()]

    def _player_records(self) -> list[tuple[int, int]]:
        result: list[tuple[int, int]] = []
        entity_list = self.pm.read_longlong(self.client + self.dw_entity_list)
        if not entity_list:
            return result
        for index in range(1, 64):
            entry = self.pm.read_longlong(entity_list + 8 * ((index & 0x7FFF) >> 9) + 16)
            if not entry:
                continue
            controller = self.pm.read_longlong(entry + 112 * (index & 0x1FF))
            if not controller:
                continue
            handle = self.pm.read_int(controller + self.player_pawn)
            if not handle:
                continue
            pawn_entry = self.pm.read_longlong(entity_list + 8 * ((handle & 0x7FFF) >> 9) + 16)
            if pawn_entry:
                pawn = self.pm.read_longlong(pawn_entry + 112 * (handle & 0x1FF))
                if pawn:
                    result.append((controller, pawn))
        return result

    def _entity_from_handle(self, handle: int) -> int:
        entity_list = self.pm.read_longlong(self.client + self.dw_entity_list)
        if not entity_list or not handle:
            return 0
        entry = self.pm.read_longlong(entity_list + 8 * ((handle & 0x7FFF) >> 9) + 16)
        return self.pm.read_longlong(entry + 112 * (handle & 0x1FF)) if entry else 0

    @staticmethod
    def _argb(red: int, green: int, blue: int, alpha: int = 180) -> int:
        unsigned = (alpha << 24) | (blue << 16) | (green << 8) | red
        return ctypes.c_int32(unsigned).value

    def _color(self, settings: Settings, health: int) -> int:
        if settings.health_color:
            health = max(0, min(100, health))
            return self._argb(int(255 * (1 - health / 100)), int(255 * health / 100), 0)
        value = settings.custom_color if valid_hex_color(settings.custom_color) else "#ffffff"
        return self._argb(int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))

    def glow_loop(self) -> None:
        self._guarded_loop("Glow", self._glow_tick, 0.016)

    def _glow_tick(self) -> None:
        settings = self.state.get()
        if not settings.enabled or not settings.glow:
            self._pause(0.12)
            return
        local = self.pm.read_longlong(self.client + self.dw_local_player)
        if not local:
            self._pause(0.12)
            return
        local_team = self.pm.read_int(local + self.team)
        for pawn in self._pawns():
            if self.pm.read_int(pawn + self.life_state) != 256:
                continue
            if self.pm.read_int(pawn + self.team) == local_team:
                continue
            color = self._color(settings, self.pm.read_int(pawn + self.health))
            glow = pawn + self.glow
            self.pm.write_int(glow + self.glow_color, color)
            self.pm.write_int(glow + self.glow_type, 3)
            self.pm.write_bool(glow + self.glowing, True)

    def anti_flash_loop(self) -> None:
        self._guarded_loop("Anti-Flash", self._flash_tick, 0.03)

    def _flash_tick(self) -> None:
        settings = self.state.get()
        if not settings.enabled or not settings.anti_flash:
            self._pause(0.12)
            return
        local = self.pm.read_longlong(self.client + self.dw_local_player)
        if local:
            self.pm.write_float(local + self.flash_duration, 0.0)

    def bunny_hop_loop(self) -> None:
        self._guarded_loop("Bunny Hop", self._bhop_tick, 0.004)

    def _bhop_tick(self) -> None:
        settings = self.state.get()
        if not settings.enabled or not settings.bunny_hop or not keyboard.is_pressed("space"):
            self._pause(0.02)
            return
        local = self.pm.read_longlong(self.client + self.dw_local_player)
        if local and self.pm.read_int(local + self.flags) & 1:
            keyboard.send("space")
            self._pause(0.025)

    def no_recoil_loop(self) -> None:
        """Compensate the weapon's real aim-punch pattern during a burst."""
        old_pitch = old_yaw = 0.0
        last_log = 0.0
        while not self.stop.is_set():
            try:
                settings = self.state.get()
                local = self.pm.read_longlong(self.client + self.dw_local_player)
                shots = self.pm.read_int(local + self.shots_fired) if local else 0
                if not settings.enabled or not settings.no_recoil or shots <= 0:
                    old_pitch = old_yaw = 0.0
                    self._pause(0.015)
                    continue

                punch_service = self.pm.read_longlong(local + self.aim_punch_services)
                if not punch_service or punch_service < 0x10000:
                    old_pitch = old_yaw = 0.0
                    self._pause(0.015)
                    continue
                # Current recoil is split into the weapon pattern and random spread.
                punch_pitch = (
                    self.pm.read_float(punch_service + self.predictable_punch)
                    + self.pm.read_float(punch_service + self.unpredictable_punch)
                )
                punch_yaw = (
                    self.pm.read_float(punch_service + self.predictable_punch + 4)
                    + self.pm.read_float(punch_service + self.unpredictable_punch + 4)
                )
                view_pitch = self.pm.read_float(self.client + self.dw_view_angles)
                view_yaw = self.pm.read_float(self.client + self.dw_view_angles + 4)
                strength = max(0.0, min(1.0, settings.recoil_strength / 100.0))
                pitch_correction = (old_pitch - punch_pitch) * 2.0 * strength
                yaw_correction = (old_yaw - punch_yaw) * 2.0 * strength
                # Reject corrupt service values instead of snapping the camera.
                if abs(pitch_correction) > 20.0 or abs(yaw_correction) > 20.0:
                    old_pitch, old_yaw = punch_pitch, punch_yaw
                    self._pause(0.01)
                    continue
                new_pitch = max(-89.0, min(89.0, view_pitch + pitch_correction))
                new_yaw = view_yaw + yaw_correction
                while new_yaw > 180.0:
                    new_yaw -= 360.0
                while new_yaw < -180.0:
                    new_yaw += 360.0
                if new_pitch == new_pitch and new_yaw == new_yaw:
                    self.pm.write_float(self.client + self.dw_view_angles, new_pitch)
                    self.pm.write_float(self.client + self.dw_view_angles + 4, new_yaw)
                    old_pitch, old_yaw = punch_pitch, punch_yaw
            except Exception:
                now = time.monotonic()
                if now - last_log > 3:
                    logging.exception("Ошибка модуля No Recoil")
                    last_log = now
                old_pitch = old_yaw = 0.0
                self._pause(0.1)
            self._pause(0.006)

    def no_shake_loop(self) -> None:
        def clear_view_punch() -> None:
            settings = self.state.get()
            if not settings.enabled or not settings.no_shake:
                self._pause(0.06)
                return
            local = self.pm.read_longlong(self.client + self.dw_local_player)
            if not local or self.pm.read_int(local + self.shots_fired) <= 0:
                self._pause(0.02)
                return
            camera = self.pm.read_longlong(local + self.camera_services) if local else 0
            if camera and camera > 0x10000:
                for offset in (0, 4, 8):
                    address = camera + self.view_punch + offset
                    if abs(self.pm.read_float(address)) > 0.0001:
                        self.pm.write_float(address, 0.0)

        self._guarded_loop("No Shake", clear_view_punch, 0.012)

    @staticmethod
    def _normalize_angle(value: float) -> float:
        while value > 180.0:
            value -= 360.0
        while value < -180.0:
            value += 360.0
        return value

    def vector_aim_loop(self) -> None:
        """Aim at the enemy closest to the crosshair while Alt is held."""
        def aim_tick() -> None:
            settings = self.state.get()
            if not settings.enabled or not settings.aim_enabled or not keyboard.is_pressed("alt"):
                self._pause(0.012)
                return
            local = self.pm.read_longlong(self.client + self.dw_local_player)
            if not local:
                return
            local_node = self.pm.read_longlong(local + self.scene_node)
            if not local_node:
                return
            lx = self.pm.read_float(local_node + self.abs_origin)
            ly = self.pm.read_float(local_node + self.abs_origin + 4)
            lz = self.pm.read_float(local_node + self.abs_origin + 8) + 64.0
            current_pitch = self.pm.read_float(self.client + self.dw_view_angles)
            current_yaw = self.pm.read_float(self.client + self.dw_view_angles + 4)
            local_team = self.pm.read_int(local + self.team)
            best: tuple[float, float, float] | None = None
            target_heights = {"head": 64.0, "neck": 58.0, "chest": 48.0, "body": 40.0}
            target_height = target_heights.get(settings.aim_target, 64.0)

            for pawn in self._pawns():
                if pawn == local or self.pm.read_int(pawn + self.life_state) != 256:
                    continue
                if self.pm.read_int(pawn + self.team) == local_team:
                    continue
                node = self.pm.read_longlong(pawn + self.scene_node)
                if not node:
                    continue
                dx = self.pm.read_float(node + self.abs_origin) - lx
                dy = self.pm.read_float(node + self.abs_origin + 4) - ly
                dz = self.pm.read_float(node + self.abs_origin + 8) + target_height - lz
                horizontal = math.hypot(dx, dy)
                if horizontal < 0.01:
                    continue
                target_pitch = -math.degrees(math.atan2(dz, horizontal))
                target_yaw = math.degrees(math.atan2(dy, dx))
                pitch_delta = self._normalize_angle(target_pitch - current_pitch)
                yaw_delta = self._normalize_angle(target_yaw - current_yaw)
                angular_distance = math.hypot(pitch_delta, yaw_delta)
                if angular_distance <= settings.aim_fov and (best is None or angular_distance < best[0]):
                    best = angular_distance, pitch_delta, yaw_delta

            if best:
                smooth = max(1.0, settings.aim_smooth)
                new_pitch = max(-89.0, min(89.0, current_pitch + best[1] / smooth))
                new_yaw = self._normalize_angle(current_yaw + best[2] / smooth)
                self.pm.write_float(self.client + self.dw_view_angles, new_pitch)
                self.pm.write_float(self.client + self.dw_view_angles + 4, new_yaw)

        self._guarded_loop("Vector Aim", aim_tick, 0.004)

    def radar_loop(self) -> None:
        def radar_tick() -> None:
            settings = self.state.get()
            if not settings.enabled or not settings.radar_hack:
                self._pause(0.12)
                return
            local = self.pm.read_longlong(self.client + self.dw_local_player)
            if not local:
                return
            local_team = self.pm.read_int(local + self.team)
            for pawn in self._pawns():
                if self.pm.read_int(pawn + self.life_state) == 256 and self.pm.read_int(pawn + self.team) != local_team:
                    self.pm.write_bool(pawn + self.spotted_state + self.spotted, True)

        self._guarded_loop("Radar Hack", radar_tick, 0.05)

    def _guarded_loop(self, name: str, action: Callable[[], None], interval: float) -> None:
        last_log = 0.0
        while not self.stop.is_set():
            try:
                action()
            except Exception:
                now = time.monotonic()
                if now - last_log > 3:
                    logging.exception("Ошибка модуля %s", name)
                    last_log = now
                self._pause(0.1)
            self._pause(interval)


class FovOverlay:
    KEY = "#010101"

    def __init__(self, root: tk.Tk, cheats: Cheats, state: StateStore):
        self.root, self.cheats, self.state = root, cheats, state
        self.game_hwnd = 0
        self.snapshot_lock = threading.Lock()
        self.snapshot: list[tuple[float, float, float, int, int, str, str, float]] = []
        self.snapshot_matrix: list[float] = []
        self.frame_counter = 0
        self.fps_value = 0
        self.fps_timer = time.monotonic()
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.configure(bg=self.KEY)
        self.window.wm_attributes("-transparentcolor", self.KEY)
        self.canvas = tk.Canvas(self.window, bg=self.KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.window.update_idletasks()
        # winfo_id may point to Tk's child wrapper; styles must be applied to
        # the real top-level HWND or the transparent canvas will eat clicks.
        get_ancestor = ctypes.windll.user32.GetAncestor
        get_ancestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        get_ancestor.restype = ctypes.c_void_p
        hwnd = get_ancestor(self.window.winfo_id(), 2)
        self.overlay_hwnd = hwnd or self.window.winfo_id()
        get_style = ctypes.windll.user32.GetWindowLongPtrW
        set_style = ctypes.windll.user32.SetWindowLongPtrW
        get_style.argtypes = (ctypes.c_void_p, ctypes.c_int)
        get_style.restype = ctypes.c_ssize_t
        set_style.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t)
        set_style.restype = ctypes.c_ssize_t
        style = get_style(self.overlay_hwnd, -20)
        set_style(self.overlay_hwnd, -20, style | 0x20 | 0x80 | 0x80000 | 0x08000000)
        set_window_pos = ctypes.windll.user32.SetWindowPos
        set_window_pos.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_uint)
        set_window_pos.restype = ctypes.c_bool
        set_window_pos(
            self.overlay_hwnd, -1, 0, 0, 0, 0,
            0x0001 | 0x0002 | 0x0010 | 0x0020 | 0x0040,
        )
        ctypes.windll.user32.GetForegroundWindow.restype = ctypes.c_void_p
        threading.Thread(target=self._collect_loop, name="esp-snapshot", daemon=True).start()
        root.after(50, self.update)

    def _collect_loop(self) -> None:
        while not self.cheats.stop.is_set():
            try:
                settings = self.state.get()
                if not settings.enabled or not settings.box_esp:
                    with self.snapshot_lock:
                        self.snapshot = []
                    self.cheats.stop.wait(0.10)
                    continue
                local = self.cheats.pm.read_longlong(self.cheats.client + self.cheats.dw_local_player)
                if not local:
                    self.cheats.stop.wait(0.05)
                    continue
                local_team = self.cheats.pm.read_int(local + self.cheats.team)
                local_node = self.cheats.pm.read_longlong(local + self.cheats.scene_node)
                lx = self.cheats.pm.read_float(local_node + self.cheats.abs_origin) if local_node else 0.0
                ly = self.cheats.pm.read_float(local_node + self.cheats.abs_origin + 4) if local_node else 0.0
                lz = self.cheats.pm.read_float(local_node + self.cheats.abs_origin + 8) if local_node else 0.0
                entities: list[tuple[float, float, float, int, int, str, str, float]] = []
                for controller, pawn in self.cheats._player_records():
                    if pawn == local or self.cheats.pm.read_int(pawn + self.cheats.life_state) != 256:
                        continue
                    if self.cheats.pm.read_int(pawn + self.cheats.team) == local_team:
                        continue
                    node = self.cheats.pm.read_longlong(pawn + self.cheats.scene_node)
                    if not node:
                        continue
                    px = self.cheats.pm.read_float(node + self.cheats.abs_origin)
                    py = self.cheats.pm.read_float(node + self.cheats.abs_origin + 4)
                    pz = self.cheats.pm.read_float(node + self.cheats.abs_origin + 8)
                    hp = max(0, min(100, self.cheats.pm.read_int(pawn + self.cheats.health)))
                    armor = max(0, min(100, self.cheats.pm.read_int(pawn + self.cheats.armor)))
                    name_ptr = self.cheats.pm.read_longlong(controller + self.cheats.player_name)
                    name = self.cheats.pm.read_string(name_ptr, 32) if name_ptr > 0x10000 else "Enemy"
                    services = self.cheats.pm.read_longlong(pawn + self.cheats.weapon_services)
                    handle = self.cheats.pm.read_int(services + self.cheats.active_weapon) if services else 0
                    weapon = self.cheats._entity_from_handle(handle)
                    weapon_name = "Weapon"
                    if weapon:
                        item = weapon + self.cheats.attribute_manager + self.cheats.econ_item
                        definition = self.cheats.pm.read_short(item + self.cheats.item_definition) & 0xFFFF
                        weapon_name = WEAPON_NAMES_BY_ID.get(definition, "Weapon")
                    distance = math.sqrt((px-lx)**2 + (py-ly)**2 + (pz-lz)**2) / 52.49
                    entities.append((px, py, pz, hp, armor, name[:24], weapon_name, distance))
                with self.snapshot_lock:
                    self.snapshot = entities
                    self.snapshot_matrix = [self.cheats.pm.read_float(
                        self.cheats.client + self.cheats.dw_view_matrix + i * 4) for i in range(16)]
            except Exception:
                pass
            self.cheats.stop.wait(0.012)

    def _rect(self) -> tuple[int, int, int, int] | None:
        if not self.game_hwnd or not ctypes.windll.user32.IsWindow(self.game_hwnd):
            windows: list[int] = []
            callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            def callback(hwnd: int, _data: int) -> bool:
                pid = ctypes.c_ulong()
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == self.cheats.pm.process_id and ctypes.windll.user32.IsWindowVisible(hwnd):
                    windows.append(hwnd)
                    return False
                return True
            ctypes.windll.user32.EnumWindows(callback_type(callback), 0)
            if not windows:
                return None
            self.game_hwnd = windows[0]
        rect, point = ctypes.wintypes.RECT(), ctypes.wintypes.POINT(0, 0)
        ctypes.windll.user32.GetClientRect(self.game_hwnd, ctypes.byref(rect))
        ctypes.windll.user32.ClientToScreen(self.game_hwnd, ctypes.byref(point))
        return point.x, point.y, rect.right, rect.bottom

    @staticmethod
    def _project(position: tuple[float, float, float], matrix: list[float],
                 width: int, height: int) -> tuple[float, float] | None:
        x, y, z = position
        clip_x = x * matrix[0] + y * matrix[1] + z * matrix[2] + matrix[3]
        clip_y = x * matrix[4] + y * matrix[5] + z * matrix[6] + matrix[7]
        clip_w = x * matrix[12] + y * matrix[13] + z * matrix[14] + matrix[15]
        if clip_w <= 0.01:
            return None
        return width * 0.5 * (1 + clip_x / clip_w), height * 0.5 * (1 - clip_y / clip_w)

    def update(self) -> None:
        try:
            settings = self.state.get()
            foreground = ctypes.windll.user32.GetForegroundWindow()
            game_active = bool(self.game_hwnd and foreground == self.game_hwnd)
            if not self.game_hwnd:
                self._rect()  # Resolve it once before checking the foreground window.
                foreground = ctypes.windll.user32.GetForegroundWindow()
                game_active = bool(self.game_hwnd and foreground == self.game_hwnd)
            draw_fov = settings.aim_enabled and settings.show_fov
            extras = settings.crosshair_enabled or settings.watermark or settings.overlay_fps
            rect = self._rect() if settings.enabled and (draw_fov or settings.box_esp or extras) and game_active else None
            if not rect:
                self.window.withdraw()
            else:
                x, y, width, height = rect
                self.window.geometry(f"{width}x{height}+{x}+{y}")
                self.window.deiconify()
                self.canvas.delete("all")
                self.frame_counter += 1
                now = time.monotonic()
                if now - self.fps_timer >= 1.0:
                    self.fps_value = round(self.frame_counter / (now - self.fps_timer))
                    self.frame_counter = 0
                    self.fps_timer = now
                if settings.watermark:
                    self.canvas.create_rectangle(12, 12, 164, 36, fill="#11131b", outline=settings.fov_color)
                    self.canvas.create_text(22, 24, anchor="w", text="MILKY WAY  •  CS2",
                                            fill="#f3f3f7", font=("Segoe UI", 8, "bold"))
                if settings.overlay_fps:
                    self.canvas.create_text(width-12, 16, anchor="ne", text=f"ESP {self.fps_value} FPS",
                                            fill=settings.name_color, font=("Consolas", 8, "bold"))
                if settings.crosshair_enabled:
                    cx, cy, size = width/2, height/2, settings.crosshair_size
                    self.canvas.create_line(cx-size, cy, cx-2, cy, fill=settings.crosshair_color, width=1)
                    self.canvas.create_line(cx+2, cy, cx+size, cy, fill=settings.crosshair_color, width=1)
                    self.canvas.create_line(cx, cy-size, cx, cy-2, fill=settings.crosshair_color, width=1)
                    self.canvas.create_line(cx, cy+2, cx, cy+size, fill=settings.crosshair_color, width=1)
                if draw_fov:
                    radius = math.tan(math.radians(settings.aim_fov)) / math.tan(math.radians(45.0)) * width / 2
                    cx, cy = width / 2, height / 2
                    self.canvas.create_oval(cx-radius, cy-radius, cx+radius, cy+radius, outline=settings.fov_color, width=1)
                if settings.box_esp:
                    with self.snapshot_lock:
                        entities = list(self.snapshot)
                        matrix = list(self.snapshot_matrix)
                    if len(matrix) == 16:
                      for px, py, pz, hp, armor, name, weapon_name, distance in entities:
                        feet = self._project((px, py, pz), matrix, width, height)
                        head = self._project((px, py, pz + 64.0), matrix, width, height)
                        if not feet or not head:
                            continue
                        box_h = abs(feet[1] - head[1])
                        if box_h < 5 or box_h > height:
                            continue
                        box_w = box_h * 0.46
                        color = settings.box_color
                        left, right = head[0]-box_w/2, head[0]+box_w/2
                        if settings.esp_fill:
                            self.canvas.create_rectangle(left, head[1], right, feet[1], fill=settings.box_color,
                                                         outline="", stipple="gray25")
                        self.canvas.create_rectangle(left, head[1], right, feet[1], outline=color, width=2)
                        if settings.esp_name:
                            self.canvas.create_text(head[0], head[1]-9, text=name, fill=settings.name_color,
                                                    font=("Segoe UI", 8, "bold"))
                        if settings.esp_health:
                            bar_x = left - 6
                            self.canvas.create_rectangle(bar_x-2, head[1], bar_x+1, feet[1], fill="#17171b", outline="")
                            hp_top = feet[1] - box_h * hp / 100
                            hp_color = settings.hp_color if not settings.health_color else f"#{255-hp*255//100:02x}{hp*255//100:02x}30"
                            self.canvas.create_rectangle(bar_x-1, hp_top, bar_x, feet[1], fill=hp_color, outline="")
                            if hp < 100:
                                self.canvas.create_text(bar_x-5, hp_top, text=str(hp), anchor="e", fill="#ffffff",
                                                        font=("Segoe UI", 7, "bold"))
                        if settings.esp_armor and armor > 0:
                            armor_y = feet[1] + 4
                            self.canvas.create_rectangle(left, armor_y, right, armor_y+2, fill="#181a20", outline="")
                            self.canvas.create_rectangle(left, armor_y, left + box_w * armor / 100,
                                                         armor_y+2, fill=settings.armor_color, outline="")
                        if settings.esp_weapon:
                            self.canvas.create_text(head[0], feet[1]+12, text=f"▸ {weapon_name}", fill=settings.weapon_color,
                                                    font=("Segoe UI Symbol", 8))
                        if settings.esp_distance:
                            self.canvas.create_text(head[0], feet[1]+24, text=f"{distance:.0f} m", fill=settings.name_color,
                                                    font=("Segoe UI", 7))
                        if settings.esp_snapline:
                            self.canvas.create_line(width/2, height-2, head[0], feet[1], fill=settings.box_color, width=1)
                        if settings.esp_head_dot:
                            dot = max(2.0, box_w * 0.08)
                            self.canvas.create_oval(head[0]-dot, head[1]-dot, head[0]+dot, head[1]+dot,
                                                    fill=settings.box_color, outline="")
        except Exception:
            self.window.withdraw()
        if not self.cheats.stop.is_set():
            self.window.after(16, self.update)


class Menu:
    BG = "#090a0f"
    PANEL = "#11131b"
    PANEL_2 = "#171a24"
    TEXT = "#f3f3f7"
    MUTED = "#777d91"
    ACCENT = "#9b5cff"
    RED = "#ff416c"

    def __init__(self, cheats: Cheats, state: StateStore, stop: threading.Event, status: str):
        self.state = state
        self.stop = stop
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.geometry("720x510")
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.visible = True
        self.events: queue.SimpleQueue[str] = queue.SimpleQueue()

        current = state.get()
        self.enabled_var = tk.BooleanVar(value=current.enabled)
        self.glow_var = tk.BooleanVar(value=current.glow)
        self.flash_var = tk.BooleanVar(value=current.anti_flash)
        self.bhop_var = tk.BooleanVar(value=current.bunny_hop)
        self.recoil_var = tk.BooleanVar(value=current.no_recoil)
        self.recoil_strength_var = tk.DoubleVar(value=current.recoil_strength)
        self.shake_var = tk.BooleanVar(value=current.no_shake)
        self.aim_var = tk.BooleanVar(value=current.aim_enabled)
        self.smooth_var = tk.DoubleVar(value=current.aim_smooth)
        self.fov_var = tk.DoubleVar(value=current.aim_fov)
        self.target_var = tk.StringVar(value=current.aim_target)
        self.show_fov_var = tk.BooleanVar(value=current.show_fov)
        self.box_var = tk.BooleanVar(value=current.box_esp)
        self.esp_name_var = tk.BooleanVar(value=current.esp_name)
        self.esp_health_var = tk.BooleanVar(value=current.esp_health)
        self.esp_weapon_var = tk.BooleanVar(value=current.esp_weapon)
        self.esp_armor_var = tk.BooleanVar(value=current.esp_armor)
        self.esp_distance_var = tk.BooleanVar(value=current.esp_distance)
        self.esp_snapline_var = tk.BooleanVar(value=current.esp_snapline)
        self.esp_head_dot_var = tk.BooleanVar(value=current.esp_head_dot)
        self.element_colors = {
            "Box": tk.StringVar(value=current.box_color), "Name": tk.StringVar(value=current.name_color),
            "HP": tk.StringVar(value=current.hp_color), "Armor": tk.StringVar(value=current.armor_color),
            "Weapon": tk.StringVar(value=current.weapon_color), "FOV": tk.StringVar(value=current.fov_color),
            "Crosshair": tk.StringVar(value=current.crosshair_color),
        }
        self.radar_var = tk.BooleanVar(value=current.radar_hack)
        self.crosshair_var = tk.BooleanVar(value=current.crosshair_enabled)
        self.crosshair_size_var = tk.DoubleVar(value=current.crosshair_size)
        self.watermark_var = tk.BooleanVar(value=current.watermark)
        self.overlay_fps_var = tk.BooleanVar(value=current.overlay_fps)
        self.esp_fill_var = tk.BooleanVar(value=current.esp_fill)
        self.color_mode = tk.StringVar(value="health" if current.health_color else "custom")
        self.custom_color = current.custom_color

        self._styles()
        self._build(status)
        self.fov_overlay = FovOverlay(self.root, cheats, state)
        self._bind_hotkeys()
        self.root.after(100, self._poll_events)

    def _styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TCheckbutton", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI", 10))
        style.map("TCheckbutton", background=[("active", self.PANEL)], foreground=[("active", self.TEXT)])
        style.configure("TRadiobutton", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI", 10))
        style.map("TRadiobutton", background=[("active", self.PANEL)], foreground=[("active", self.TEXT)])
        style.configure("Aim.TCombobox", fieldbackground="#202330", background="#202330",
                        foreground=self.TEXT, arrowcolor=self.ACCENT, bordercolor="#2b2e3d", padding=6)
        style.map("Aim.TCombobox", fieldbackground=[("readonly", "#202330")],
                  foreground=[("readonly", self.TEXT)], selectbackground=[("readonly", "#202330")])

    def _build(self, status: str) -> None:
        top = tk.Frame(self.root, bg=self.BG, height=58)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(top, text="MILKY", fg=self.TEXT, bg=self.BG, font=("Segoe UI", 17, "bold")).pack(side="left", padx=(22, 0))
        tk.Label(top, text="WAY", fg=self.ACCENT, bg=self.BG, font=("Segoe UI", 17, "bold")).pack(side="left")
        tk.Label(top, text="●  CS2 CONNECTED", fg="#43d9a3", bg=self.BG, font=("Segoe UI", 8, "bold")).pack(side="right", padx=22)

        body = tk.Frame(self.root, bg=self.BG)
        body.pack(fill="both", expand=True)
        sidebar = tk.Frame(body, bg="#0d0f16", width=150)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="MODULES", fg=self.MUTED, bg="#0d0f16", font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=20, pady=(20, 8))

        holder = tk.Frame(body, bg=self.BG)
        holder.pack(side="left", fill="both", expand=True, padx=18, pady=(4, 18))
        self.pages: dict[str, tk.Frame] = {}
        self.nav_buttons: dict[str, tk.Button] = {}
        for name, icon in (("Aim", "◈"), ("Vision", "◉"), ("Misc", "◇")):
            button = tk.Button(sidebar, text=f"{icon}   {name}", command=lambda page=name: self._show_page(page),
                               anchor="w", relief="flat", bd=0, padx=14, pady=11, cursor="hand2",
                               font=("Segoe UI", 10, "bold"), bg="#0d0f16", fg=self.MUTED,
                               activebackground="#1a1725", activeforeground=self.ACCENT)
            button.pack(fill="x", padx=8, pady=2)
            self.nav_buttons[name] = button
            page = tk.Frame(holder, bg=self.BG)
            self.pages[name] = page

        tk.Checkbutton(sidebar, text="MASTER", variable=self.enabled_var, command=self._sync, bg="#0d0f16",
                       fg=self.ACCENT, activebackground="#0d0f16", activeforeground=self.ACCENT,
                       selectcolor="#20182f", font=("Segoe UI", 9, "bold")).pack(side="bottom", anchor="w", padx=18, pady=(4, 18))
        tk.Label(sidebar, text="F1 MENU  •  F2 MASTER", fg="#4e5364", bg="#0d0f16", font=("Consolas", 8)).pack(side="bottom", pady=4)

        aim = self.pages["Aim"]
        self._page_title(aim, "Aim", "Weapon and target control")
        aim_left = self._group(aim, "VECTOR AIM", "Hold ALT to activate")
        aim_left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        aim_right = self._group(aim, "RECOIL", "Weapon handling")
        aim_right.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self._check(aim_left, "Enable Vector Aim", self.aim_var)
        self._check(aim_left, "Show FOV circle", self.show_fov_var)
        self._combo(aim_left, "Aim point", self.target_var, ("head", "neck", "chest", "body"))
        self._slider(aim_left, "Smooth", self.smooth_var, 1.0, 20.0, 1.0)
        self._slider(aim_left, "FOV", self.fov_var, 1.0, 30.0, 1.0)
        self._check(aim_right, "No Recoil", self.recoil_var)
        self._slider(aim_right, "Strength", self.recoil_strength_var, 0.0, 100.0, 5.0)
        self._check(aim_right, "Remove screen shake", self.shake_var)

        vision = self.pages["Vision"]
        self._page_title(vision, "Vision", "Players and local view")
        players = self._group(vision, "PLAYERS", "Enemy visualization")
        players.pack(side="left", fill="both", expand=True, padx=(0, 6))
        local = self._group(vision, "STYLE / EXTRAS", "ESP palette and overlay extras")
        local.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self._check(players, "Glow ESP", self.glow_var)
        self._check(players, "Box ESP", self.box_var)
        self._check(players, "Name", self.esp_name_var)
        self._check(players, "Health bar", self.esp_health_var)
        self._check(players, "Weapon", self.esp_weapon_var)
        self._check(players, "Armor", self.esp_armor_var)
        self._check(players, "Distance", self.esp_distance_var)
        self._check(players, "Snapline", self.esp_snapline_var)
        self._check(players, "Head dot", self.esp_head_dot_var)
        modes = tk.Frame(players, bg=self.PANEL)
        modes.pack(fill="x", padx=16, pady=5)
        ttk.Radiobutton(modes, text="Health", variable=self.color_mode, value="health", command=self._sync).pack(side="left")
        ttk.Radiobutton(modes, text="Custom", variable=self.color_mode, value="custom", command=self._sync).pack(side="left", padx=6)
        self.color_button = tk.Button(modes, text=" ", bg=self.custom_color, width=3, relief="flat", command=self.choose_color)
        self.color_button.pack(side="right")
        self._color_palette(local)
        self._check(local, "Filled box", self.esp_fill_var)
        self._check(local, "Custom crosshair", self.crosshair_var)
        self._slider(local, "Crosshair", self.crosshair_size_var, 3.0, 18.0, 1.0)
        self._check(local, "Watermark", self.watermark_var)
        self._check(local, "Overlay FPS", self.overlay_fps_var)

        misc = self.pages["Misc"]
        self._page_title(misc, "Misc", "Movement and effects")
        movement = self._group(misc, "MOVEMENT", "Player movement")
        movement.pack(side="left", fill="both", expand=True, padx=(0, 6))
        effects = self._group(misc, "EFFECTS", "Screen effects")
        effects.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self._check(movement, "Bunny Hop", self.bhop_var)
        self._check(effects, "Anti-Flash", self.flash_var)
        self._check(effects, "Radar Hack", self.radar_var)
        tk.Label(effects, text="Settings are saved automatically.", fg=self.MUTED, bg=self.PANEL, wraplength=180,
                 justify="left", font=("Segoe UI", 8)).pack(anchor="w", padx=16, pady=12)

        self._show_page("Aim")

    def _page_title(self, parent: tk.Frame, title: str, subtitle: str) -> None:
        header = tk.Frame(parent, bg=self.BG)
        header.pack(fill="x", pady=(0, 10))
        tk.Label(header, text=title, fg=self.TEXT, bg=self.BG, font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(header, text=subtitle, fg=self.MUTED, bg=self.BG, font=("Segoe UI", 8)).pack(anchor="w")

    def _show_page(self, name: str) -> None:
        for page_name, page in self.pages.items():
            page.pack_forget()
            active = page_name == name
            self.nav_buttons[page_name].configure(bg="#1a1725" if active else "#0d0f16",
                                                  fg=self.ACCENT if active else self.MUTED)
        self.pages[name].pack(fill="both", expand=True)
    def _group(self, parent: tk.Frame, title: str, subtitle: str) -> tk.Frame:
        group = tk.Frame(parent, bg=self.PANEL, highlightbackground="#242735", highlightthickness=1)
        tk.Label(group, text=title, fg=self.ACCENT, bg=self.PANEL, font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(14, 1))
        tk.Label(group, text=subtitle, fg=self.MUTED, bg=self.PANEL, font=("Segoe UI", 8)).pack(anchor="w", padx=16, pady=(0, 10))
        tk.Frame(group, bg="#292c39", height=1).pack(fill="x", padx=16, pady=(0, 7))
        return group
    def _check(self, parent: tk.Frame, text: str, variable: tk.BooleanVar) -> None:
        ttk.Checkbutton(parent, text=text, variable=variable, command=self._sync).pack(anchor="w", padx=16, pady=7)

    def _slider(self, parent: tk.Frame, text: str, variable: tk.DoubleVar,
                minimum: float, maximum: float, resolution: float) -> None:
        row = tk.Frame(parent, bg=self.PANEL)
        row.pack(fill="x", padx=16, pady=1)
        tk.Label(row, text=text, fg=self.MUTED, bg=self.PANEL, font=("Segoe UI", 9), width=10, anchor="w").pack(side="left")
        tk.Scale(row, variable=variable, from_=minimum, to=maximum, resolution=resolution,
                 orient="horizontal", showvalue=True, command=lambda _value: self._sync(),
                 bg=self.PANEL, fg=self.TEXT, troughcolor="#303030", activebackground=self.ACCENT,
                 highlightthickness=0, bd=0, length=140).pack(side="right")

    def _combo(self, parent: tk.Frame, text: str, variable: tk.StringVar, values: tuple[str, ...]) -> None:
        row = tk.Frame(parent, bg=self.PANEL)
        row.pack(fill="x", padx=16, pady=5)
        tk.Label(row, text=text, fg=self.MUTED, bg=self.PANEL, font=("Segoe UI", 9)).pack(side="left")
        combo = ttk.Combobox(row, textvariable=variable, state="readonly", width=11,
                             values=values, style="Aim.TCombobox")
        combo.pack(side="right")
        combo.bind("<<ComboboxSelected>>", lambda _event: self._sync())

    def _color_palette(self, parent: tk.Frame) -> None:
        tk.Label(parent, text="ESP COLORS", fg=self.MUTED, bg=self.PANEL,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=16, pady=(8, 3))
        grid = tk.Frame(parent, bg=self.PANEL)
        grid.pack(fill="x", padx=14, pady=(0, 6))
        self.element_color_buttons: dict[str, tk.Button] = {}
        for index, (name, variable) in enumerate(self.element_colors.items()):
            button = tk.Button(grid, text=name, command=lambda key=name: self._choose_element_color(key),
                               bg=variable.get(), fg="#ffffff", activebackground=variable.get(),
                               activeforeground="#ffffff", relief="flat", font=("Segoe UI", 8, "bold"),
                               cursor="hand2", width=8)
            button.grid(row=index // 3, column=index % 3, padx=2, pady=2, sticky="ew")
            grid.grid_columnconfigure(index % 3, weight=1)
            self.element_color_buttons[name] = button

    def _choose_element_color(self, name: str) -> None:
        variable = self.element_colors[name]
        selected = colorchooser.askcolor(initialcolor=variable.get(), title=f"{name} color", parent=self.root)[1]
        if selected:
            variable.set(selected.lower())
            self.element_color_buttons[name].configure(bg=selected, activebackground=selected)
            self._sync()

    def _sync(self) -> None:
        settings = self.state.set(
            enabled=self.enabled_var.get(),
            glow=self.glow_var.get(),
            anti_flash=self.flash_var.get(),
            bunny_hop=self.bhop_var.get(),
            no_recoil=self.recoil_var.get(),
            recoil_strength=float(self.recoil_strength_var.get()),
            no_shake=self.shake_var.get(),
            aim_enabled=self.aim_var.get(),
            aim_smooth=float(self.smooth_var.get()),
            aim_fov=float(self.fov_var.get()),
            aim_target=self.target_var.get(),
            show_fov=self.show_fov_var.get(),
            box_esp=self.box_var.get(),
            esp_name=self.esp_name_var.get(),
            esp_health=self.esp_health_var.get(),
            esp_weapon=self.esp_weapon_var.get(),
            esp_armor=self.esp_armor_var.get(),
            esp_distance=self.esp_distance_var.get(),
            esp_snapline=self.esp_snapline_var.get(),
            esp_head_dot=self.esp_head_dot_var.get(),
            box_color=self.element_colors["Box"].get(),
            name_color=self.element_colors["Name"].get(),
            hp_color=self.element_colors["HP"].get(),
            armor_color=self.element_colors["Armor"].get(),
            weapon_color=self.element_colors["Weapon"].get(),
            fov_color=self.element_colors["FOV"].get(),
            crosshair_enabled=self.crosshair_var.get(),
            crosshair_color=self.element_colors["Crosshair"].get(),
            crosshair_size=float(self.crosshair_size_var.get()),
            watermark=self.watermark_var.get(),
            overlay_fps=self.overlay_fps_var.get(),
            esp_fill=self.esp_fill_var.get(),
            radar_hack=self.radar_var.get(),
            health_color=self.color_mode.get() == "health",
            custom_color=self.custom_color,
        )
        try:
            settings.save()
        except OSError:
            logging.exception("Не удалось сохранить настройки")

    def choose_color(self) -> None:
        selected = colorchooser.askcolor(initialcolor=self.custom_color, title="Цвет подсветки", parent=self.root)[1]
        if selected:
            self.custom_color = selected.lower()
            self.color_button.configure(bg=selected, activebackground=selected)
            self.color_mode.set("custom")
            self._sync()

    def _bind_hotkeys(self) -> None:
        try:
            keyboard.add_hotkey("f1", lambda: self.events.put("toggle"), suppress=False)
            keyboard.add_hotkey("f2", lambda: self.events.put("master"), suppress=False)
        except Exception:
            logging.exception("Глобальная клавиша F1 недоступна")
        self.root.bind("<Escape>", lambda _event: self.toggle())

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event == "toggle":
                    self.toggle()
                elif event == "master":
                    self.enabled_var.set(not self.enabled_var.get())
                    self._sync()
        except queue.Empty:
            pass
        if not self.stop.is_set():
            self.root.after(100, self._poll_events)

    def toggle(self) -> None:
        if self.visible:
            self.root.withdraw()
        else:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        self.visible = not self.visible

    def close(self) -> None:
        if self.stop.is_set():
            return
        self._sync()
        self.stop.set()
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        self.root.after(80, self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()


def connect() -> tuple[pymem.Pymem, int]:
    pm = pymem.Pymem(PROCESS_NAME)
    module = pymem.process.module_from_name(pm.process_handle, "client.dll")
    if module is None:
        raise RuntimeError("Модуль client.dll не найден")
    return pm, module.lpBaseOfDll


def show_startup_error(message: str) -> None:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(APP_NAME, message, parent=root)
    root.destroy()


def main() -> int:
    configure_logging()
    logging.info("Запуск %s %s", APP_NAME, APP_VERSION)
    try:
        pm, client = connect()
        state = StateStore(Settings.load())
        stop = threading.Event()
        cheats = Cheats(pm, client, state, stop)
    except requests.RequestException as exc:
        logging.exception("Ошибка загрузки смещений")
        show_startup_error(f"Не удалось загрузить актуальные смещения.\n\n{exc}\n\nПодробности: {LOG_PATH}")
        return 2
    except (pymem.exception.PymemError, ProcessLookupError, RuntimeError) as exc:
        logging.exception("Ошибка подключения")
        show_startup_error(f"Не удалось подключиться к {PROCESS_NAME}.\nСначала запустите игру.\n\n{exc}")
        return 3
    except (OffsetError, KeyError, TypeError) as exc:
        logging.exception("Некорректные смещения")
        show_startup_error(f"Формат смещений изменился.\n\n{exc}\n\nПодробности: {LOG_PATH}")
        return 4

    workers = (
        threading.Thread(target=cheats.glow_loop, name="glow", daemon=True),
        threading.Thread(target=cheats.anti_flash_loop, name="anti-flash", daemon=True),
        threading.Thread(target=cheats.bunny_hop_loop, name="bunny-hop", daemon=True),
        threading.Thread(target=cheats.no_recoil_loop, name="no-recoil", daemon=True),
        threading.Thread(target=cheats.no_shake_loop, name="no-shake", daemon=True),
        threading.Thread(target=cheats.vector_aim_loop, name="vector-aim", daemon=True),
        threading.Thread(target=cheats.radar_loop, name="radar", daemon=True),
    )
    for worker in workers:
        worker.start()

    try:
        Menu(cheats, state, stop, f"Подключено к {PROCESS_NAME}").run()
    finally:
        stop.set()
        for worker in workers:
            worker.join(timeout=0.4)
        try:
            pm.close_process()
        except Exception:
            logging.exception("Ошибка закрытия процесса")
        logging.info("Завершение программы")
    return 0


if __name__ == "__main__":
    sys.exit(main())
