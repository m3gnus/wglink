"""Document-local mesh evidence learned from WG; no Fusion imports or polling.

Read on an explicit command, never by the heartbeat. Only the exact submission's
status can teach a fraction. A changed document keeps just the historical words.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import time
from typing import Any, Mapping

if __package__:
    from . import wglink_inbox as inbox, wglink_protocol as protocol
else:
    import wglink_inbox as inbox
    import wglink_protocol as protocol

FILENAME = 'wglink-mesh-history.json'
_CALIBRATION = []


def digest(value: Any) -> str:
    return hashlib.sha256(protocol.canonical_json(value, protocol.UTF8_STRICT).encode('utf-8')).hexdigest()


def settings_hash(mesh: Mapping[str, Any], sources: list) -> str:
    # Origins and measured areas are not mesh dials. Membership and geometry
    # have their own key; source ids/roles and zone selectors belong here too.
    return digest({
        'mesh': {key: mesh.get(key) for key in ('wall_size_mm', 'grading_distance_mm', 'symmetry', 'source_size_mm')},
        'groups': [{key: row.get(key) for key in ('group_id', 'size_mm', 'selectors')}
                   for row in mesh.get('surface_groups', [])],
        'sources': sorted([[row.get('id'), row.get('role'), row.get('selectors')] for row in sources], key=str),
    })


def _load(folder: Path) -> list:
    payload = inbox._read_json(folder / FILENAME)
    rows = payload.get('records', []) if isinstance(payload, dict) else []
    now = time.time()
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get('at'), (int, float))
            and now - row['at'] <= 30 * 86400 and all(isinstance(row.get(key), str) for key in
                ('request', 'manifest', 'document', 'geometry', 'settings'))
            and isinstance(row.get('estimate'), dict) and type(row['estimate'].get('full')) is int
            and row.get('drawnFraction', 1.0) in (.25, .5, 1.0)
            and ('mesh' not in row or protocol._status_mesh(row['mesh']) is not None)][-50:]


def remember(folder: Path, request_id: str, manifest_hash: str, document: str | None,
             geometry: Any, mesh: Mapping[str, Any], sources: list, estimate: Mapping[str, Any], *, drawn_fraction: float = 1.0) -> None:
    if not document or geometry is None:
        return
    rows = _load(folder)
    rows.append({'request': request_id, 'manifest': manifest_hash, 'document': digest(document),
                 'geometry': digest(geometry), 'settings': settings_hash(mesh, sources),
                 'estimate': dict(estimate), 'drawnFraction': drawn_fraction, 'at': time.time()})
    inbox._write_json_atomically(folder / FILENAME, {'records': rows[-50:]})


def learn(folder: Path | None, document: str | None, geometry: Any,
          mesh: Mapping[str, Any], sources: list) -> dict | None:
    if folder is None or not document:
        return None
    rows = _load(folder)
    changed = False
    for row in rows:
        if row.get('mesh') is not None or row.get('document') != digest(document):
            continue
        request = row.get('request')
        if not isinstance(request, str) or not inbox._PLAIN_ID.fullmatch(request):
            continue
        payload = inbox._read_json(folder / inbox.ACK_DIRECTORY / (request + protocol.REQUEST_STATUS_SUFFIX))
        status = protocol.validate_request_status(payload, request_id=request)
        if status is None or status.get('manifestSha256') != row.get('manifest'):
            continue
        if status['state'] != 'solved' and not (status['state'] == 'needsYou' and
                status.get('prompt', {}).get('code') == protocol.MESH_TOO_LARGE_CODE):
            continue
        actual = status.get('result', {}).get('mesh')
        if actual is not None:
            row['mesh'] = actual
            fraction = min(1.0, actual['solvedFraction'] / row.get('drawnFraction', 1.0))
            # Calibration belongs to diagnostics, never to the user's words.
            predicted = round(row['estimate']['full'] * fraction)
            row['calibration'] = {'estimated': predicted, 'actual': actual['trianglesSolved'],
                                  'relativeDifference': predicted / actual['trianglesSolved'] - 1}
            changed = True
    _CALIBRATION[:] = [row['calibration'] for row in rows if isinstance(row.get('calibration'), dict)]
    if changed:
        inbox._write_json_atomically(folder / FILENAME, {'records': rows})
    found = next((row for row in reversed(rows) if row.get('document') == digest(document) and row.get('mesh')), None)
    if found is None:
        return None
    return {**found, 'matches': geometry is not None and found['geometry'] == digest(geometry)
            and found['settings'] == settings_hash(mesh, sources)}


def matching_fraction(learned: Mapping[str, Any] | None) -> float | None:
    return min(1.0, learned['mesh']['solvedFraction'] / learned.get('drawnFraction', 1.0)) if learned and learned.get('matches') else None


def learned_text(learned: Mapping[str, Any] | None) -> str | None:
    if not learned:
        return None
    mesh = learned['mesh']
    kind = mesh.get('domainKind')
    label = {'quarter': 'quarter model', 'half': 'half model', 'full': 'full model'}.get(kind, 'model')
    if learned.get('matches'):
        return f"Last WG mesh: {mesh['trianglesSolved']:,} triangles ({label})."
    return f"Last time {protocol.APP_NAME} used a {label}."


def calibration_diagnostics() -> list:
    """Cached pairs for the existing diagnostics payload; never reads Fusion."""
    return list(_CALIBRATION)
