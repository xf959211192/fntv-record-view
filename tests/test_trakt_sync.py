import unittest
from contextlib import nullcontext
from unittest.mock import patch

from main import (
    _calculate_play_progress,
    app,
    _build_trakt_dashboard,
    _build_trakt_auth_headers,
    _build_trakt_status,
    _build_trakt_history_payload,
    _clamp_percentage,
    _compare_titles,
    _derive_watch_state,
    _get_trakt_show_episode,
    _get_trakt_token_expire_at,
    _match_movie_item,
    _match_episode_item,
    _normalize_trakt_auto_sync_user_guid,
    _normalize_imdb_id,
    _parse_external_ids,
    _sync_failed_queue_candidate,
    _to_trakt_datetime,
    _validate_movie_candidate,
    _validate_episode_candidate,
)


class TraktSyncTestCase(unittest.TestCase):
    def test_get_app_meta(self):
        with app.test_client() as client:
            response = client.get('/api/meta')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn('version', payload)
        self.assertIn('commit_sha', payload)
        self.assertIn('commit_short', payload)

    def test_calculate_play_progress(self):
        self.assertEqual(_calculate_play_progress(0, 30), 0.0)
        self.assertEqual(_calculate_play_progress(None, 30), 0.0)
        self.assertEqual(_calculate_play_progress(1800, 30), 100.0)
        self.assertEqual(_calculate_play_progress(900, 30), 50.0)
        self.assertEqual(_calculate_play_progress(999999, 30), 100.0)

    def test_derive_watch_state(self):
        self.assertEqual(_derive_watch_state(0), 'unwatched')
        self.assertEqual(_derive_watch_state(12.5), 'in_progress')
        self.assertEqual(_derive_watch_state(100), 'watched')

    def test_normalize_imdb_id(self):
        self.assertEqual(_normalize_imdb_id('1234567'), 'tt1234567')
        self.assertEqual(_normalize_imdb_id('tt7654321'), 'tt7654321')
        self.assertIsNone(_normalize_imdb_id(''))

    def test_to_trakt_datetime(self):
        self.assertEqual(_to_trakt_datetime(0), '1970-01-01T00:00:00Z')
        self.assertIsNone(_to_trakt_datetime(None))

    def test_build_payload_movie_and_episode(self):
        rows = [
            {
                'update_time': 0,
                'season_number': None,
                'episode_number': None,
                'item_trakt_id': 100,
                'item_imdb_id': None,
                'item_tmdb_id': None,
                'item_tvdb_id': None,
                'item_slug': None,
            },
            {
                'update_time': 1000,
                'season_number': 1,
                'episode_number': 2,
                'item_trakt_id': None,
                'item_imdb_id': 'tt1111111',
                'item_tmdb_id': None,
                'item_tvdb_id': None,
                'item_slug': None,
            },
        ]

        payload, skipped = _build_trakt_history_payload(rows)
        self.assertEqual(len(payload['movies']), 1)
        self.assertEqual(len(payload['episodes']), 1)
        self.assertEqual(skipped['skipped_no_ids'], 0)
        self.assertEqual(skipped['skipped_no_time'], 0)

    def test_build_payload_skip_without_ids(self):
        rows = [
            {
                'update_time': 1000,
                'season_number': None,
                'episode_number': None,
                'item_trakt_id': None,
                'item_imdb_id': None,
                'item_tmdb_id': None,
                'item_tvdb_id': None,
                'item_slug': None,
            }
        ]

        payload, skipped = _build_trakt_history_payload(rows)
        self.assertEqual(len(payload['movies']), 0)
        self.assertEqual(len(payload['episodes']), 0)
        self.assertEqual(skipped['skipped_no_ids'], 1)

    def test_get_trakt_token_expire_at(self):
        token_data = {
            'created_at': 100,
            'expires_in': 3600
        }
        self.assertEqual(_get_trakt_token_expire_at(token_data), 3700)

    def test_build_trakt_auth_headers(self):
        headers = _build_trakt_auth_headers('abc123')
        self.assertEqual(headers['Authorization'], 'Bearer abc123')
        self.assertEqual(headers['trakt-api-version'], '2')

    def test_build_trakt_status_without_token(self):
        status = _build_trakt_status()
        self.assertIn('configured', status)
        self.assertIn('connected', status)
        self.assertIn('last_sync', status)
        self.assertIn('auto_sync_enabled', status)
        self.assertIn('auto_sync_interval_seconds', status)
        self.assertIn('auto_sync_watched_threshold', status)
        self.assertIn('auto_sync_limit', status)
        self.assertIn('auto_sync_user_guid', status)
        self.assertIn('auto_sync_user_display', status)

    def test_parse_external_ids(self):
        raw = '{"ids":{"imdb":"tt1234567","tmdb":888},"episodes":[{"source":"tvdb","id":999}]}'
        parsed = _parse_external_ids(raw)
        self.assertEqual(parsed['imdb'], 'tt1234567')
        self.assertEqual(parsed['tmdb'], 888)
        self.assertEqual(parsed['tvdb'], 999)

    def test_compare_titles(self):
        self.assertGreaterEqual(_compare_titles('Fine, thank you,and you?', 'Fine thank you and you'), 0.75)
        self.assertLess(_compare_titles('成何体统', 'Great Hotels'), 0.75)

    def test_clamp_percentage(self):
        self.assertEqual(_clamp_percentage(120), 100)
        self.assertEqual(_clamp_percentage(0), 1)
        self.assertEqual(_clamp_percentage('88'), 88)

    def test_normalize_trakt_auto_sync_user_guid(self):
        self.assertEqual(_normalize_trakt_auto_sync_user_guid('  user-1  '), 'user-1')
        self.assertEqual(_normalize_trakt_auto_sync_user_guid(None), '')

    def test_validate_episode_candidate_show_ids_should_override_title_difference(self):
        item = {
            'season_number': 1,
            'episode_number': 183,
            'episode_title': '最强的剑',
            'show_tmdb_id': 12345,
            'show_imdb_id': 'tt9999999',
            'episode_imdb_id': None,
            'episode_external_ids': {},
        }
        show = {
            'ids': {
                'trakt': 1,
                'tmdb': 12345,
                'imdb': 'tt9999999',
            }
        }
        episode = {
            'season': 1,
            'number': 183,
            'title': 'The Strongest Sword',
            'ids': {
                'trakt': 1001,
            }
        }

        confidence, notes = _validate_episode_candidate(item, show, episode, 'show_tmdb_season_episode')

        self.assertEqual(confidence, 'high')
        self.assertIn('标题差异较大', notes)
        self.assertIn('show tmdb 一致', notes)
        self.assertIn('show imdb 一致', notes)
        self.assertIn('show 主键强匹配，标题降权处理', notes)

    def test_validate_episode_candidate_episode_id_should_override_title_difference(self):
        item = {
            'season_number': 99,
            'episode_number': 99,
            'episode_title': '最强的剑',
            'show_tmdb_id': None,
            'show_imdb_id': None,
            'episode_imdb_id': 'tt1234567',
            'episode_external_ids': {},
        }
        show = {'ids': {}}
        episode = {
            'season': 1,
            'number': 183,
            'title': 'The Strongest Sword',
            'ids': {
                'trakt': 1001,
                'imdb': 'tt1234567',
            }
        }

        confidence, notes = _validate_episode_candidate(item, show, episode, 'episode_imdb')

        self.assertEqual(confidence, 'high')
        self.assertIn('episode_imdb_id 一致', notes)
        self.assertIn('episode 主键强匹配，忽略弱特征差异', notes)

    def test_validate_movie_candidate_id_should_override_title_and_year_difference(self):
        item = {
            'title': '中文片名',
            'original_title': '',
            'year': 2020,
            'tmdb_id': 12345,
            'imdb_id': None,
        }
        movie = {
            'title': 'English Movie Title',
            'year': 2021,
            'ids': {
                'trakt': 2001,
                'tmdb': 12345,
            }
        }

        confidence, notes = _validate_movie_candidate(item, movie, 'tmdb_id')

        self.assertEqual(confidence, 'high')
        self.assertIn('tmdb 一致', notes)
        self.assertIn('movie 主键强匹配，忽略弱特征差异', notes)

    @patch('main._upsert_match_cache')
    @patch('main._get_cached_match')
    def test_match_episode_item_should_revalidate_cached_low_confidence(self, mock_get_cached_match, mock_upsert_match_cache):
        mock_get_cached_match.return_value = {
            'trakt_episode_id': 1001,
            'trakt_show_id': 2002,
            'matched_by': 'show_tmdb_season_episode',
            'confidence': 'low',
            'raw_match_snapshot': '{"show":{"ids":{"trakt":2002,"tmdb":12345,"imdb":"tt9999999"},"title":"神印王座"},"episode":{"ids":{"trakt":1001},"season":1,"number":183,"title":"The Strongest Sword"}}'
        }
        item = {
            'item_guid': 'item-1',
            'watched_at': '2026-03-23T10:00:00Z',
            'season_number': 1,
            'episode_number': 183,
            'episode_title': '最强的剑',
            'show_tmdb_id': 12345,
            'show_imdb_id': 'tt9999999',
            'episode_imdb_id': None,
            'episode_external_ids': {},
        }

        matched_entry, matched_by, confidence, trakt_show_id, notes = _match_episode_item(item)

        self.assertEqual(matched_entry, {
            'ids': {'trakt': 1001},
            'watched_at': '2026-03-23T10:00:00Z'
        })
        self.assertEqual(matched_by, 'show_tmdb_season_episode')
        self.assertEqual(confidence, 'high')
        self.assertEqual(trakt_show_id, 2002)
        self.assertIn('show 主键强匹配，标题降权处理', notes)
        self.assertIn('命中本地缓存', notes)
        mock_upsert_match_cache.assert_called_once()

    @patch('main._upsert_match_cache')
    @patch('main._get_cached_match')
    def test_match_movie_item_should_revalidate_cached_low_confidence(self, mock_get_cached_match, mock_upsert_match_cache):
        mock_get_cached_match.return_value = {
            'trakt_movie_id': 3003,
            'matched_by': 'tmdb_id',
            'confidence': 'low',
            'raw_match_snapshot': '{"movie":{"ids":{"trakt":3003,"tmdb":12345},"title":"English Movie Title","year":2021}}'
        }
        item = {
            'item_guid': 'movie-1',
            'watched_at': '2026-03-23T10:00:00Z',
            'title': '中文片名',
            'original_title': '',
            'year': 2020,
            'tmdb_id': 12345,
            'imdb_id': None,
        }

        matched_entry, matched_by, confidence, notes = _match_movie_item(item)

        self.assertEqual(matched_entry, {
            'ids': {'trakt': 3003},
            'watched_at': '2026-03-23T10:00:00Z'
        })
        self.assertEqual(matched_by, 'tmdb_id')
        self.assertEqual(confidence, 'high')
        self.assertIn('movie 主键强匹配，忽略弱特征差异', notes)
        self.assertIn('命中本地缓存', notes)
        mock_upsert_match_cache.assert_called_once()

    @patch('main._match_trakt_items')
    @patch('main._update_failed_queue_item')
    @patch('main._build_normalized_media_items')
    @patch('main._fetch_single_record_for_trakt_sync')
    @patch('main.get_db_connection')
    @patch('main._get_failed_queue_item')
    def test_rematch_failed_item_should_bypass_cache_for_low_confidence(
        self,
        mock_get_failed_queue_item,
        mock_get_db_connection,
        mock_fetch_single_record_for_trakt_sync,
        mock_build_normalized_media_items,
        mock_update_failed_queue_item,
        mock_match_trakt_items,
    ):
        mock_get_failed_queue_item.return_value = {
            'reason': 'low_confidence'
        }
        mock_get_db_connection.return_value = nullcontext(object())
        mock_fetch_single_record_for_trakt_sync.return_value = object()
        mock_build_normalized_media_items.return_value = [{'type': 'movie', 'item_guid': 'item-1', 'title': '中文片名'}]
        mock_match_trakt_items.return_value = (
            {'movies': [{'ids': {'trakt': 1}, 'watched_at': '2026-03-23T10:00:00Z'}], 'episodes': []},
            {'skipped_low_confidence': 0},
            [{
                'remote_id': 1,
                'remote_title': 'English Movie Title',
                'matched_by': 'tmdb_id',
                'confidence': 'high',
                'ids': {'trakt': 1},
                'validation_notes': ['tmdb 一致', 'movie 主键强匹配，忽略弱特征差异'],
                'item_type': 'movie'
            }]
        )

        with app.test_client() as client:
            response = client.post('/api/trakt/failed_queue/rematch', json={
                'user_guid': 'user-1',
                'item_guid': 'item-1'
            })

        self.assertEqual(response.status_code, 200)
        mock_match_trakt_items.assert_called_once_with(
            [{'type': 'movie', 'item_guid': 'item-1', 'title': '中文片名'}],
            bypass_cache=True
        )
        mock_update_failed_queue_item.assert_called_once()
        refreshed_payload = mock_update_failed_queue_item.call_args.kwargs['candidate_payload']
        self.assertEqual(refreshed_payload['confidence'], 'high')
        self.assertEqual(refreshed_payload['candidate_title'], 'English Movie Title')

    @patch('main._match_trakt_items')
    @patch('main._update_failed_queue_item')
    @patch('main._build_normalized_media_items')
    @patch('main._fetch_single_record_for_trakt_sync')
    @patch('main.get_db_connection')
    @patch('main._get_failed_queue_item')
    def test_rematch_failed_item_should_resolve_already_synced_queue_item(
        self,
        mock_get_failed_queue_item,
        mock_get_db_connection,
        mock_fetch_single_record_for_trakt_sync,
        mock_build_normalized_media_items,
        mock_update_failed_queue_item,
        mock_match_trakt_items,
    ):
        mock_get_failed_queue_item.return_value = {
            'reason': 'unmatched'
        }
        mock_get_db_connection.return_value = nullcontext(object())
        mock_fetch_single_record_for_trakt_sync.return_value = object()
        mock_build_normalized_media_items.return_value = [{'type': 'episode', 'item_guid': 'item-2', 'title': '剧集标题'}]
        mock_match_trakt_items.return_value = (
            {'movies': [], 'episodes': []},
            {'skipped_already_synced': 1, 'skipped_low_confidence': 0, 'skipped_unmatched': 0, 'skipped_no_ids': 0, 'skipped_no_time': 0},
            []
        )

        with app.test_client() as client:
            response = client.post('/api/trakt/failed_queue/rematch', json={
                'user_guid': 'user-2',
                'item_guid': 'item-2'
            })

        self.assertEqual(response.status_code, 200)
        self.assertIn('已同步', (response.get_json() or {}).get('message', ''))
        mock_update_failed_queue_item.assert_called_once()
        self.assertEqual(mock_update_failed_queue_item.call_args.args[2], 'resolved')

    @patch('main._load_sync_states')
    @patch('main._load_failed_queue')
    def test_build_trakt_dashboard_should_only_show_pending_failed_queue(self, mock_load_failed_queue, mock_load_sync_states):
        mock_load_failed_queue.side_effect = [
            [
                {'status': 'pending', 'item_guid': 'item-1'},
                {'status': 'pending', 'item_guid': 'item-2'},
            ],
            [
                {'status': 'pending', 'item_guid': 'item-1'},
                {'status': 'pending', 'item_guid': 'item-2'},
                {'status': 'rematched', 'item_guid': 'item-3'},
                {'status': 'resolved', 'item_guid': 'item-4'},
            ]
        ]
        mock_load_sync_states.return_value = []

        payload = _build_trakt_dashboard(20)

        self.assertEqual(len(payload['failed_queue']), 2)
        self.assertTrue(all(item['status'] == 'pending' for item in payload['failed_queue']))
        self.assertEqual(payload['summary']['failed_pending'], 2)
        self.assertEqual(payload['summary']['failed_resolved'], 1)

    @patch('main._get_cached_match')
    @patch('main._search_trakt_by_id')
    @patch('main._upsert_match_cache')
    def test_match_movie_item_should_ignore_cache_when_bypass_cache_enabled(
        self,
        mock_upsert_match_cache,
        mock_search_trakt_by_id,
        mock_get_cached_match,
    ):
        mock_get_cached_match.return_value = {
            'trakt_movie_id': 3003,
            'matched_by': 'tmdb_id',
            'confidence': 'high',
        }
        mock_search_trakt_by_id.return_value = [{
            'movie': {
                'title': 'English Movie Title',
                'year': 2021,
                'ids': {'trakt': 2001, 'tmdb': 12345}
            }
        }]
        item = {
            'item_guid': 'movie-1',
            'watched_at': '2026-03-23T10:00:00Z',
            'title': '中文片名',
            'original_title': '',
            'year': 2020,
            'tmdb_id': 12345,
            'imdb_id': None,
        }

        matched_entry, matched_by, confidence, notes = _match_movie_item(item, bypass_cache=True)

        self.assertEqual(matched_entry, {
            'ids': {'trakt': 2001},
            'watched_at': '2026-03-23T10:00:00Z'
        })
        self.assertEqual(matched_by, 'tmdb_id')
        self.assertEqual(confidence, 'high')
        self.assertIn('movie 主键强匹配，忽略弱特征差异', notes)
        mock_get_cached_match.assert_not_called()
        mock_search_trakt_by_id.assert_called_once()
        mock_upsert_match_cache.assert_called_once()

    @patch('main._update_failed_queue_status')
    @patch('main._upsert_sync_state')
    @patch('main._upsert_match_cache')
    @patch('main._push_to_trakt_history')
    @patch('main._ensure_trakt_access_token')
    @patch('main._is_trakt_configured')
    @patch('main._build_normalized_media_items')
    @patch('main._fetch_single_record_for_trakt_sync')
    @patch('main.get_db_connection')
    @patch('main._get_failed_queue_item')
    def test_sync_failed_queue_candidate_movie(
        self,
        mock_get_failed_queue_item,
        mock_get_db_connection,
        mock_fetch_single_record_for_trakt_sync,
        mock_build_normalized_media_items,
        mock_is_trakt_configured,
        mock_ensure_trakt_access_token,
        mock_push_to_trakt_history,
        mock_upsert_match_cache,
        mock_upsert_sync_state,
        mock_update_failed_queue_status,
    ):
        mock_get_failed_queue_item.return_value = {
            'candidate_payload': {
                'type': 'movie',
                'candidate_ids': {'trakt': 4321},
                'candidate_title': 'Movie Candidate',
                'match_notes': ['标题近似匹配'],
            }
        }
        mock_get_db_connection.return_value = nullcontext(object())
        mock_fetch_single_record_for_trakt_sync.return_value = object()
        mock_build_normalized_media_items.return_value = [{
            'watched_at': '2026-03-23T10:00:00Z'
        }]
        mock_is_trakt_configured.return_value = True
        mock_ensure_trakt_access_token.return_value = 'token'
        mock_push_to_trakt_history.return_value = (201, {'added': {'movies': 1}})

        status_code, payload = _sync_failed_queue_candidate('user-1', 'item-1')

        self.assertEqual(status_code, 200)
        self.assertEqual(payload['media_type'], 'movie')
        self.assertEqual(payload['trakt_id'], 4321)
        mock_push_to_trakt_history.assert_called_once_with(
            {
                'movies': [{'ids': {'trakt': 4321}, 'watched_at': '2026-03-23T10:00:00Z'}],
                'episodes': []
            },
            unittest.mock.ANY,
            'token'
        )
        mock_upsert_match_cache.assert_called_once()
        mock_upsert_sync_state.assert_called_once()
        mock_update_failed_queue_status.assert_called_once()

    @patch('main._update_failed_queue_status')
    @patch('main._upsert_sync_state')
    @patch('main._upsert_match_cache')
    @patch('main._push_to_trakt_history')
    @patch('main._ensure_trakt_access_token')
    @patch('main._is_trakt_configured')
    @patch('main._build_normalized_media_items')
    @patch('main._fetch_single_record_for_trakt_sync')
    @patch('main.get_db_connection')
    @patch('main._get_failed_queue_item')
    def test_sync_failed_queue_candidate_episode(
        self,
        mock_get_failed_queue_item,
        mock_get_db_connection,
        mock_fetch_single_record_for_trakt_sync,
        mock_build_normalized_media_items,
        mock_is_trakt_configured,
        mock_ensure_trakt_access_token,
        mock_push_to_trakt_history,
        mock_upsert_match_cache,
        mock_upsert_sync_state,
        mock_update_failed_queue_status,
    ):
        mock_get_failed_queue_item.return_value = {
            'candidate_payload': {
                'type': 'episode',
                'candidate_ids': {'trakt': 9876},
                'candidate_show_id': 555,
                'candidate_title': 'Show Name - Episode Name',
                'match_notes': ['标题差异较大'],
            }
        }
        mock_get_db_connection.return_value = nullcontext(object())
        mock_fetch_single_record_for_trakt_sync.return_value = object()
        mock_build_normalized_media_items.return_value = [{
            'watched_at': '2026-03-23T11:00:00Z'
        }]
        mock_is_trakt_configured.return_value = True
        mock_ensure_trakt_access_token.return_value = 'token'
        mock_push_to_trakt_history.return_value = (201, {'added': {'episodes': 1}})

        status_code, payload = _sync_failed_queue_candidate('user-2', 'item-2')

        self.assertEqual(status_code, 200)
        self.assertEqual(payload['media_type'], 'episode')
        self.assertEqual(payload['trakt_id'], 9876)
        self.assertEqual(payload['trakt_show_id'], 555)
        mock_push_to_trakt_history.assert_called_once_with(
            {
                'movies': [],
                'episodes': [{'ids': {'trakt': 9876}, 'watched_at': '2026-03-23T11:00:00Z'}]
            },
            unittest.mock.ANY,
            'token'
        )
        mock_upsert_match_cache.assert_called_once()
        mock_upsert_sync_state.assert_called_once()
        mock_update_failed_queue_status.assert_called_once()

    def test_validate_episode_candidate_should_support_season_zero(self):
        item = {
            'season_number': 0,
            'episode_number': 1,
            'episode_title': 'OVA1',
            'show_tmdb_id': 62741,
            'show_imdb_id': 'tt2320220',
            'episode_imdb_id': None,
            'episode_external_ids': {},
        }
        show = {
            'ids': {
                'trakt': 67789,
                'tmdb': 62741,
                'imdb': 'tt2320220',
            }
        }
        episode = {
            'season': 0,
            'number': 1,
            'title': 'The God Was Discarded',
            'ids': {
                'trakt': 1131442,
            }
        }

        confidence, notes = _validate_episode_candidate(item, show, episode, 'show_tmdb_season_episode')

        self.assertEqual(confidence, 'high')
        self.assertIn('show tmdb 一致', notes)
        self.assertIn('show imdb 一致', notes)

    @patch('main._ensure_trakt_access_token')
    @patch('main._trakt_get')
    def test_get_trakt_show_episode_should_support_season_zero(self, mock_trakt_get, mock_ensure_trakt_access_token):
        mock_ensure_trakt_access_token.return_value = 'token'
        mock_trakt_get.return_value = (200, [
            {
                'number': 0,
                'episodes': [
                    {'season': 0, 'number': 1, 'title': 'The God Was Discarded', 'ids': {'trakt': 1131442}}
                ]
            },
            {
                'number': 1,
                'episodes': []
            }
        ])

        episode = _get_trakt_show_episode(67789, 0, 1)

        self.assertIsNotNone(episode)
        self.assertEqual(episode['ids']['trakt'], 1131442)


if __name__ == '__main__':
    unittest.main()
