import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from tests.conftest import commit_valid, preview_valid


def _history_map(client):
    return {v['version_id']: v for v in client.get('/api/history').json()['versions']}


def _migrate_twice(client):
    """gen0(empty) --M1--> gen1(3 rows) --M2--> gen2(4 rows)."""
    commit_valid(client, preview_valid(client))
    client.post('/api/legacy', json={'legacy_id': 4, 'code': 'D-004', 'raw_name': 'Delta', 'note': None})
    second = commit_valid(client, preview_valid(client))
    return second


def test_restore_preview_reports_rows_constraints_generation_and_diff(client):
    _migrate_twice(client)
    state = client.get('/api/state').json()
    assert state['formal_generation'] == 2
    assert [row['id'] for row in state['records']] == [1, 2, 3, 4]

    # Version 2 holds the 3-row formal table sealed by the second migration.
    response = client.post('/api/restores/preview', json={'version_id': 2})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['ok'] is True
    assert body['version_id'] == 2
    assert body['source_kind'] == 'migration'
    assert body['base_generation'] == 2
    assert body['current_generation'] == 2
    assert body['source_row_count'] == 3
    assert body['candidate_row_count'] == 3

    diff = body['diff']
    assert diff['current_row_count'] == 4
    assert diff['candidate_row_count'] == 3
    assert [row['id'] for row in diff['removed']] == [4]
    assert diff['added'] == []
    assert diff['changed'] == []
    assert diff['unchanged_count'] == 3

    # The rehearsal alone must not modify the formal table or its generation.
    state = client.get('/api/state').json()
    assert state['formal_generation'] == 2
    assert [row['id'] for row in state['records']] == [1, 2, 3, 4]
    assert len(state['restore_candidate_tables']) == 1
    assert len(state['restore_previews']) == 1


def test_restore_commit_seals_current_switches_candidate_and_advances_generation(client):
    _migrate_twice(client)
    preview = client.post('/api/restores/preview', json={'version_id': 2}).json()

    result = client.post(
        '/api/restores/commit',
        json={'preview_id': preview['preview_id'], 'base_generation': preview['base_generation']},
    )
    assert result.status_code == 200, result.text
    committed = result.json()
    assert committed['ok'] is True
    assert committed['source_version_id'] == 2
    assert committed['sealed_version_id'] == 3
    assert committed['sealed_row_count'] == 4
    assert committed['base_generation'] == 2
    assert committed['formal_generation'] == 3
    assert committed['row_count'] == 3

    records = client.get('/api/records').json()['rows']
    assert [row['id'] for row in records] == [1, 2, 3]

    versions = _history_map(client)
    assert set(versions) == {1, 2, 3}
    assert versions[3]['kind'] == 'restore'
    assert versions[3]['source_version_id'] == 2
    assert versions[3]['row_count'] == 4
    assert versions[3]['base_generation'] == 2
    assert versions[3]['new_generation'] == 3
    assert versions[3]['locked'] == 1

    # The pre-restore formal table (4 rows) was preserved for investigation.
    sealed_rows = client.get('/api/history/3/rows').json()['rows']
    assert [row['id'] for row in sealed_rows] == [1, 2, 3, 4]
    # The read-only restore source was not modified.
    source_rows = client.get('/api/history/2/rows').json()['rows']
    assert [row['id'] for row in source_rows] == [1, 2, 3]

    state = client.get('/api/state').json()
    assert state['formal_generation'] == 3
    assert state['restore_previews'] == []
    assert state['restore_candidate_tables'] == []


def test_restore_preview_unknown_or_empty_version(client):
    missing = client.post('/api/restores/preview', json={'version_id': 99})
    assert missing.status_code == 404
    assert missing.json()['error']['code'] == 'version_not_found'

    # Version 1 is the empty formal table sealed by the first migration:
    # restoring it is valid and yields a constrained empty candidate.
    commit_valid(client, preview_valid(client))
    empty = client.post('/api/restores/preview', json={'version_id': 1})
    assert empty.status_code == 200
    body = empty.json()
    assert body['ok'] is True
    assert body['source_row_count'] == 0
    assert body['diff']['removed'] == [
        {'id': 1, 'code': 'A-001', 'label': 'Alpha', 'legacy_id': 1},
        {'id': 2, 'code': 'B-002', 'label': 'Beta', 'legacy_id': 2},
        {'id': 3, 'code': 'C-003', 'label': 'Gamma', 'legacy_id': 3},
    ]


def test_old_migration_preview_cannot_overwrite_restored_table(client):
    _migrate_twice(client)
    stale = preview_valid(client)
    assert stale['formal_generation'] == 2

    restore_preview = client.post('/api/restores/preview', json={'version_id': 2}).json()
    restored = client.post(
        '/api/restores/commit',
        json={'preview_id': restore_preview['preview_id']},
    )
    assert restored.status_code == 200
    assert restored.json()['formal_generation'] == 3

    # The migration rehearsal saw gen 2; after the restore it must be rejected
    # even though the legacy source revision is unchanged (old client payload,
    # no formal_generation field: still server-adjudicated).
    replay = client.post(
        '/api/migrations/commit',
        json={'preview_id': stale['preview_id'], 'source_revision': stale['source_revision']},
    )
    assert replay.status_code == 409
    assert replay.json()['error']['code'] == 'stale_generation'

    state = client.get('/api/state').json()
    assert [row['id'] for row in state['records']] == [1, 2, 3]
    assert state['previews'] == []
    assert state['shadow_tables'] == []

    # A fresh rehearsal on gen 3 commits normally.
    committed = commit_valid(client, preview_valid(client))
    assert committed['base_generation'] == 3
    assert committed['formal_generation'] == 4
    assert [row['id'] for row in client.get('/api/records').json()['rows']] == [1, 2, 3, 4]


def test_old_restore_preview_is_rejected_after_interleaved_migration(client):
    _migrate_twice(client)
    stale_restore = client.post('/api/restores/preview', json={'version_id': 2}).json()
    assert stale_restore['base_generation'] == 2

    # A migration lands while the operator is reviewing the restore rehearsal.
    client.post('/api/legacy', json={'legacy_id': 5, 'code': 'E-005', 'raw_name': 'Echo', 'note': None})
    committed = commit_valid(client, preview_valid(client))
    assert committed['formal_generation'] == 3

    replay = client.post(
        '/api/restores/commit',
        json={'preview_id': stale_restore['preview_id'], 'base_generation': 2},
    )
    assert replay.status_code == 409
    assert replay.json()['error']['code'] == 'stale_generation'

    state = client.get('/api/state').json()
    # The interleaved migration's 5-row formal table is intact.
    assert [row['id'] for row in state['records']] == [1, 2, 3, 4, 5]
    assert state['restore_previews'] == []
    assert state['restore_candidate_tables'] == []
    # No orphan sealed version was produced.
    assert [v['version_id'] for v in state['history']] == [1, 2, 3]


def test_two_competing_restores_only_one_wins(client):
    _migrate_twice(client)
    first_preview = client.post('/api/restores/preview', json={'version_id': 2}).json()
    second_preview = client.post('/api/restores/preview', json={'version_id': 2}).json()
    assert first_preview['preview_id'] != second_preview['preview_id']
    assert first_preview['base_generation'] == second_preview['base_generation'] == 2

    winner = client.post(
        '/api/restores/commit',
        json={'preview_id': first_preview['preview_id'], 'base_generation': 2},
    )
    assert winner.status_code == 200
    assert winner.json()['formal_generation'] == 3

    loser = client.post(
        '/api/restores/commit',
        json={'preview_id': second_preview['preview_id'], 'base_generation': 2},
    )
    assert loser.status_code == 409
    assert loser.json()['error']['code'] == 'stale_generation'

    state = client.get('/api/state').json()
    assert state['formal_generation'] == 3
    assert state['restore_previews'] == []
    assert state['restore_candidate_tables'] == []
    assert [row['id'] for row in state['records']] == [1, 2, 3]
    # Only the winner sealed a pre-restore version.
    assert [v['version_id'] for v in state['history']] == [1, 2, 3]

    # The winner's consumed rehearsal cannot be replayed either.
    replay = client.post(
        '/api/restores/commit',
        json={'preview_id': first_preview['preview_id'], 'base_generation': 3},
    )
    assert replay.status_code == 404
    assert replay.json()['error']['code'] == 'restore_preview_not_found'


def test_restore_is_chainable_and_second_restore_reseals(client):
    _migrate_twice(client)

    def restore(version_id):
        preview = client.post('/api/restores/preview', json={'version_id': version_id}).json()
        response = client.post(
            '/api/restores/commit', json={'preview_id': preview['preview_id']}
        )
        assert response.status_code == 200, response.text
        return response.json()

    first = restore(2)   # gen 2 -> 3, formal content 4 rows -> 3 rows
    # Version 3 is what the first restore sealed: the pre-restore 4-row table.
    assert first['sealed_row_count'] == 4
    second = restore(3)  # restore that sealed snapshot: gen 3 -> 4, 3 rows -> 4
    assert second['source_version_id'] == 3
    assert second['sealed_version_id'] == 4
    assert second['base_generation'] == 3
    assert second['formal_generation'] == 4
    assert second['row_count'] == 4  # version 3 held the sealed 4-row table
    assert second['sealed_row_count'] == 3

    versions = _history_map(client)
    assert versions[4]['kind'] == 'restore'
    assert versions[4]['source_version_id'] == 3
    assert versions[4]['row_count'] == 3
    assert [row['id'] for row in client.get('/api/records').json()['rows']] == [1, 2, 3, 4]


def test_restore_switch_failure_rolls_back_entirely(client):
    _migrate_twice(client)
    preview = client.post('/api/restores/preview', json={'version_id': 2}).json()

    assert client.post(
        '/api/test/faults', json={'name': 'restore_switch', 'enabled': True}
    ).status_code == 200
    response = client.post(
        '/api/restores/commit', json={'preview_id': preview['preview_id']}
    )
    assert response.status_code == 500
    assert 'injected failure' in response.json()['error']['message']

    state = client.get('/api/state').json()
    # No half formal table, no orphan sealed version, generation unmoved.
    assert [row['id'] for row in state['records']] == [1, 2, 3, 4]
    assert state['formal_generation'] == 2
    assert [v['version_id'] for v in state['history']] == [1, 2]
    assert state['restore_previews'] == []
    assert state['restore_candidate_tables'] == []

    # The old rehearsal was invalidated: repeat submission cannot land later.
    replay = client.post(
        '/api/restores/commit', json={'preview_id': preview['preview_id']}
    )
    assert replay.status_code == 404
    assert replay.json()['error']['code'] == 'restore_preview_not_found'

    # A fresh rehearsal still restores cleanly after the fault is cleared.
    assert client.post(
        '/api/test/faults', json={'name': 'restore_switch', 'enabled': False}
    ).status_code == 200
    fresh = client.post('/api/restores/preview', json={'version_id': 2}).json()
    ok = client.post('/api/restores/commit', json={'preview_id': fresh['preview_id']})
    assert ok.status_code == 200
    assert [row['id'] for row in client.get('/api/records').json()['rows']] == [1, 2, 3]


def test_restore_copy_failure_leaves_no_candidate(client):
    _migrate_twice(client)
    assert client.post(
        '/api/test/faults', json={'name': 'restore_copy', 'enabled': True}
    ).status_code == 200
    response = client.post('/api/restores/preview', json={'version_id': 2})
    assert response.status_code == 500
    assert 'injected failure' in response.json()['error']['message']

    state = client.get('/api/state').json()
    assert state['formal_generation'] == 2
    assert [row['id'] for row in state['records']] == [1, 2, 3, 4]
    assert state['restore_previews'] == []
    assert state['restore_candidate_tables'] == []


def test_history_and_generation_survive_restart(client):
    committed = _migrate_twice(client)
    restore_preview = client.post('/api/restores/preview', json={'version_id': 2}).json()
    client.post('/api/restores/commit', json={'preview_id': restore_preview['preview_id']})

    # Simulate a server restart against the same database file.
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as restarted:
        history = restarted.get('/api/history').json()
        assert history['formal_generation'] == 3
        assert [
            (v['version_id'], v['kind'], v['source_version_id'], v['new_generation'], v['locked'])
            for v in history['versions']
        ] == [
            (1, 'migration', None, 1, 1),
            (2, 'migration', None, 2, 1),
            (3, 'restore', 2, 3, 1),
        ]
        rows = restarted.get('/api/history/3/rows').json()['rows']
        assert [row['id'] for row in rows] == [1, 2, 3, 4]
        assert [row['id'] for row in restarted.get('/api/records').json()['rows']] == [1, 2, 3]
        assert restarted.get('/api/state').json()['formal_generation'] == 3


def test_concurrent_restores_only_one_wins(client):
    from app.main import app

    _migrate_twice(client)
    first_preview = client.post('/api/restores/preview', json={'version_id': 2}).json()
    second_preview = client.post('/api/restores/preview', json={'version_id': 2}).json()

    # Separate TestClient instances each run requests on their own portal
    # thread, so the two confirmations genuinely contend on SQLite's write
    # lock instead of being serialized by a shared client.
    client_a = TestClient(app, raise_server_exceptions=False)
    client_b = TestClient(app, raise_server_exceptions=False)
    responses = {}
    barrier = threading.Barrier(2)

    def confirm(name, test_client, preview_id):
        barrier.wait()
        responses[name] = test_client.post(
            '/api/restores/commit', json={'preview_id': preview_id, 'base_generation': 2}
        )

    threads = [
        threading.Thread(target=confirm, args=('a', client_a, first_preview['preview_id'])),
        threading.Thread(target=confirm, args=('b', client_b, second_preview['preview_id'])),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    client_a.close()
    client_b.close()

    statuses = {name: response.status_code for name, response in responses.items()}
    assert sorted(statuses.values()) == [200, 409], {k: v.text for k, v in responses.items()}
    loser = next(response for response in responses.values() if response.status_code == 409)
    assert loser.json()['error']['code'] == 'stale_generation'

    state = client.get('/api/state').json()
    assert state['formal_generation'] == 3
    assert [row['id'] for row in state['records']] == [1, 2, 3]
    # Winner sealed exactly one pre-restore version; loser left no orphans.
    assert [v['version_id'] for v in state['history']] == [1, 2, 3]
    assert state['restore_previews'] == []
    assert state['restore_candidate_tables'] == []

    # Even the winner's consumed rehearsal cannot be replayed.
    winner_id = first_preview['preview_id'] if statuses['a'] == 200 else second_preview['preview_id']
    replay = client.post('/api/restores/commit', json={'preview_id': winner_id, 'base_generation': 3})
    assert replay.status_code == 404


def test_formal_ops_ledger_is_append_only(client):
    _migrate_twice(client)
    import os
    from app.db import default_db_path

    db_path = os.environ.get('MIGRATION_DB') or default_db_path()
    with sqlite3.connect(db_path) as direct:
        with pytest.raises(sqlite3.IntegrityError):
            direct.execute('UPDATE formal_ops SET new_generation=99 WHERE op_id=1')
        with pytest.raises(sqlite3.IntegrityError):
            direct.execute('DELETE FROM formal_ops WHERE op_id=1')
        with pytest.raises(sqlite3.IntegrityError):
            direct.execute(
                "INSERT INTO formal_ops(kind, base_generation, new_generation) "
                "VALUES ('migration', 0, 1)"
            )
