"""Rewrite the dashboard's entity references to the V0.4 ``cp_`` ids.

The mapping is derived, not hand-written: the old ids encode the room in pinyin
(``ke_ting_di_nuan`` = 客厅地暖, ``zhu_wo_di_nuan`` = 主卧地暖) and the entity in
the translated English name, while the new ids use the integration's own
``zone_id`` and internal key. Writing 52 pairs by hand would be a typo waiting to
happen, so each old reference is resolved through rules and every unmapped one is
reported rather than guessed.

The script refuses to write unless every reference resolved, so a partial rewrite
cannot leave a dashboard half-updated.
"""

from __future__ import annotations

import re
from pathlib import Path

DASH = Path("dashboards/climatepilot.yaml")

#: Room pinyin -> the integration's zone_id (which is what the new ids use).
ROOM = {
    "ke_ting_di_nuan": "living_room",
    "zhu_wo_di_nuan": "master_bedroom",
    "ci_wo_di_nuan": "guest_bedroom",
}

#: System-level entity: old translated name -> internal key.
SYSTEM = {
    "wo_de_jia_heating_state": "cp_heating_state",
    "wo_de_jia_zones_demanding_heat": "cp_active_zones",
    "wo_de_jia_suggested_supply_temperature": "cp_suggested_supply_temp",
    "wo_de_jia_outdoor_temperature": "cp_outdoor_temperature",
    "wo_de_jia_supply_temperature": "cp_supply_temperature",
    "wo_de_jia_burner_starts_24_h": "cp_burner_starts_24h",
    "wo_de_jia_last_burn_duration": "cp_last_burn_duration",
    "wo_de_jia_dominant_zone_combination": "cp_dominant_zone_combination",
    "wo_de_jia_distinct_zone_combinations_24_h": "cp_combinations_24h",
    "wo_de_jia_zone_combination_breakdown_24_h": "cp_zone_combination_count",
    "wo_de_jia_combustion_quality": "cp_cycling_quality",
    "wo_de_jia_unexplained_burner_starts_24_h": "cp_unexplained_burns_24h",
    "wo_de_jia_return_temperature": "cp_return_temperature",
    "wo_de_jia_delta_t": "cp_delta_t",
    "wo_de_jia_delta_t_quality": "cp_delta_t_quality",
    "wo_de_jia_condensing_regime": "cp_condensing_regime",
    "wo_de_jia_hydraulic_verdict": "cp_hydraulic_verdict",
    "wo_de_jia_curve_calibration_advice": "cp_curve_calibration_advice",
    "wo_de_jia_short_cycle_alert": "cp_short_cycle_alert",
    "wo_de_jia_degraded_operation": "cp_degraded",
    "wo_de_jia_probe_fault": "cp_probe_fault",
    "wo_de_jia_reassess_now": "cp_reassess",
    "wo_de_jia_manual_supply_temperature": "cp_supply_temperature_override",
    "wo_de_jia_control_mode": "cp_control_mode",
    "wo_de_jia_whole_home_heating": "cp_system_thermostat",
}

#: Per-room entity: old translated name -> internal key.
ZONE = {
    "effective_setpoint": "cp_{room}_effective_setpoint",
    "setpoint_source": "cp_{room}_setpoint_source",
    "room_temperature": "cp_{room}_zone_temperature",
    "manual_override_remaining": "cp_{room}_override_remaining",
    "manual_override_active": "cp_{room}_manual_override",
    "heat_request": "cp_{room}_heat_request",
    "comfort_verdict": "cp_{room}_comfort_verdict",
    "comfort_advice": "cp_{room}_comfort_advice",
    "overheating": "cp_{room}_overheating",
    "underheating": "cp_{room}_underheating",
    "resume_automatic": "cp_{room}_resume_auto",
    "auxiliary_heat_load": "cp_{room}_auxiliary_load",
    "room_mode": "cp_{room}_zone_mode",
}

#: Entities belonging to the user's own KNX integration - never rewritten.
KEEP = {
    "climate.livingroom_floorheating",
    "climate.master_bedroom_floorheating",
    "climate.guest_bedroom_floorheating",
}

REF = re.compile(r"\b(?:sensor|binary_sensor|select|button|number|switch|climate)\.[a-z0-9_]+")


def translate(entity_id: str) -> str | None:
    """Return the V0.4 replacement for ``entity_id``, or None if unknown."""
    if entity_id in KEEP:
        return entity_id
    domain, _, object_id = entity_id.partition(".")

    if object_id in SYSTEM:
        return f"{domain}.{SYSTEM[object_id]}"

    for pinyin, zone_id in ROOM.items():
        prefix = f"{pinyin}_"
        if not object_id.startswith(prefix):
            continue
        rest = object_id[len(prefix) :]
        if rest in ZONE:
            return f"{domain}.{ZONE[rest].format(room=zone_id)}"

    return None


def main() -> int:
    text = DASH.read_text(encoding="utf-8")
    found = sorted(set(REF.findall(text)))
    print(f"distinct references in the dashboard: {len(found)}")

    mapping: dict[str, str] = {}
    unresolved: list[str] = []
    for entity_id in found:
        replacement = translate(entity_id)
        if replacement is None:
            unresolved.append(entity_id)
        else:
            mapping[entity_id] = replacement

    if unresolved:
        print(f"\nUNRESOLVED ({len(unresolved)}) - refusing to write a partial rewrite:")
        for entity_id in unresolved:
            print(f"   {entity_id}")
        return 1

    unchanged = sum(1 for k, v in mapping.items() if k == v)
    print(f"mapped: {len(mapping)} ({unchanged} left as-is: the user's own entities)")
    print()
    for old, new in sorted(mapping.items()):
        if old != new:
            print(f"   {old:56} -> {new}")

    # Replace longest first so a shorter id cannot match inside a longer one.
    for old in sorted(mapping, key=len, reverse=True):
        new = mapping[old]
        if old != new:
            text = re.sub(rf"(?<![\w.]){re.escape(old)}(?![\w])", new, text)

    # Verify: every reference now resolves to a known id.
    remaining = set(REF.findall(text))
    allowed = set(mapping.values()) | KEEP
    stray = sorted(remaining - allowed)
    if stray:
        print(f"\nSTRAY references after rewrite: {stray}")
        return 1

    DASH.write_text(text, encoding="utf-8")
    total = len(REF.findall(text))
    print(f"\nwrote {DASH} - {total} references, all resolving")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
