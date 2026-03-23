from flask import Flask, render_template, request, jsonify
import json
import logging
import os
import random
import shutil
import sqlite3
import sys # ### NEW ###
import threading
import time
import uuid
import atexit
from contextlib import contextmanager
from datetime import datetime, timezone
from queue import Queue, Empty, Full
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.getenv('APP_LOG_PATH', 'app.log'), encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 1. 获取当前文件 (main.py) 所在的目录的绝对路径
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

def _load_project_env() -> None:
    """从项目根目录加载 .env 配置。"""
    env_path = os.path.join(BASE_DIR, '.env')
    if not os.path.exists(env_path):
        return

    try:
        with open(env_path, 'r', encoding='utf-8') as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue

                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()

                if not key:
                    continue

                if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                    value = value[1:-1]

                os.environ[key] = value
    except OSError:
        logger.exception('读取项目 .env 文件失败')

_load_project_env()

APP_RUNTIME_DIR = (os.getenv('APP_RUNTIME_DIR', BASE_DIR) or BASE_DIR).strip() or BASE_DIR
os.makedirs(APP_RUNTIME_DIR, exist_ok=True)

SRC_DB_PATH = os.getenv('SRC_DB_PATH', os.path.join(BASE_DIR, 'database', 'trimmedia.db'))
TMP_DB_PATH = os.getenv('TMP_DB_PATH', os.path.join(APP_RUNTIME_DIR, 'trimmedia_tmp.db'))


def _get_env_int(name: str, default: int) -> int:
    """读取整型环境变量。"""
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError, AttributeError):
        return default

def _get_env_bool(name: str, default: bool) -> bool:
    """读取布尔环境变量。"""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'y', 'on'}

# ### NEW: Configuration for lazy, atomic copy ###
DB_EXPIRATION_SECONDS = 60  # 数据库副本的过期时间（60秒）
_last_copy_time = 0.0       # 上次拷贝成功的时间戳，初始化为0以强制首次拷贝
_db_copy_lock = threading.Lock() # 确保只有一个线程执行拷贝操作

def _atomic_copy_database():
    """
    ### NEW ###
    Performs an atomic copy of the database from the source to the temporary path.
    This is done by copying to a new file first, then renaming it.
    """
    global _last_copy_time
    logger.info("开始执行数据库原子化拷贝...")
    
    # 临时文件名，用于原子化操作
    atomic_tmp_path = TMP_DB_PATH + ".new"

    try:
        if not os.path.exists(SRC_DB_PATH):
            logger.warning(f"源数据库文件不存在: {SRC_DB_PATH}")
            return
        
        if not os.access(SRC_DB_PATH, os.R_OK):
            logger.warning(f"源数据库文件无法读取: {SRC_DB_PATH}")
            return
            
        # 1. 拷贝到带 .new 后缀的临时文件
        shutil.copy2(SRC_DB_PATH, atomic_tmp_path)
        
        # 2. 如果拷贝成功，原子化地重命名文件
        os.replace(atomic_tmp_path, TMP_DB_PATH)
        
        # 3. 仅在完全成功后更新时间戳
        _last_copy_time = time.time()
        logger.info(f"数据库原子化拷贝成功: {SRC_DB_PATH} -> {TMP_DB_PATH}")

    except Exception as e:
        logger.error(f"数据库原子化拷贝失败: {e}")
        # 如果新文件已创建但重命名失败，清理掉
        if os.path.exists(atomic_tmp_path):
            try:
                os.remove(atomic_tmp_path)
            except OSError as rm_err:
                logger.error(f"清理临时文件 {atomic_tmp_path} 失败: {rm_err}")

def _refresh_database_copy(force: bool = False) -> Dict[str, Any]:
    """手动刷新数据库副本，供前端主动触发。"""
    global _last_copy_time

    with _db_copy_lock:
        if force:
            _last_copy_time = 0.0
        _atomic_copy_database()

    return {
        'source_db_path': SRC_DB_PATH,
        'temp_db_path': TMP_DB_PATH,
        'copied_at': int(_last_copy_time * 1000) if _last_copy_time else 0,
        'copied_at_display': format_timestamp(int(_last_copy_time * 1000)) if _last_copy_time else '',
    }


@contextmanager
def get_db_connection() -> Iterator[sqlite3.Connection]:
    """
    ### CHANGED: Implemented lazy loading with expiration and thread safety ###
    
    为每个请求创建一个新的只读数据库连接。
    在连接前，会检查临时数据库是否已过期或不存在。如果需要，会触发一次
    线程安全的、原子化的数据库拷贝。
    """
    
    # 检查数据库副本是否过期或不存在
    is_expired = (time.time() - _last_copy_time) > DB_EXPIRATION_SECONDS
    if not os.path.exists(TMP_DB_PATH) or is_expired:
        # 使用锁来防止多个请求同时触发拷贝 (Race Condition)
        with _db_copy_lock:
            # 双重检查：在获取锁后，再次检查是否需要拷贝
            # 因为可能在等待锁的时候，已经有另一个线程完成了拷贝
            is_still_expired = (time.time() - _last_copy_time) > DB_EXPIRATION_SECONDS
            if not os.path.exists(TMP_DB_PATH) or is_still_expired:
                if is_still_expired:
                    logger.info("数据库副本已过期，触发更新。")
                else:
                    logger.info("数据库副本不存在，触发更新。")
                _atomic_copy_database()

    # --- 以下为原始的连接逻辑 ---
    conn = None
    max_retries = 10
    base_delay = 0.05  # 50毫秒

    for attempt in range(max_retries):
        try:
            conn = sqlite3.connect(f"file:{TMP_DB_PATH}?mode=ro", uri=True, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            break
        except sqlite3.OperationalError:
            if attempt < max_retries - 1:
                time.sleep(base_delay)
            else:
                logger.error("多次重试后数据库仍然被锁定。")
                raise
    
    if not conn:
        raise sqlite3.OperationalError("无法建立数据库连接。")

    try:
        yield conn
    finally:
        if conn:
            conn.close()

# ===============================================================
# Flask 应用 (Flask Application)
# ===============================================================

app = Flask(__name__)
TRAKT_API_BASE = os.getenv('TRAKT_API_BASE', 'https://api.trakt.tv').rstrip('/')
APP_VERSION = (os.getenv('APP_VERSION', 'dev') or 'dev').strip()
APP_COMMIT_SHA = (os.getenv('APP_COMMIT_SHA', 'unknown') or 'unknown').strip()
APP_BUILD_TIME = (os.getenv('APP_BUILD_TIME', '') or '').strip()
TRAKT_CLIENT_ID = os.getenv('TRAKT_CLIENT_ID', '').strip()
TRAKT_CLIENT_SECRET = os.getenv('TRAKT_CLIENT_SECRET', '').strip()
TRAKT_REDIRECT_URI = os.getenv('TRAKT_REDIRECT_URI', 'urn:ietf:wg:oauth:2.0:oob').strip()
TRAKT_TOKEN_PATH = os.getenv('TRAKT_TOKEN_PATH', os.path.join(APP_RUNTIME_DIR, 'trakt_tokens.json'))
TRAKT_LAST_SYNC_PATH = os.getenv('TRAKT_LAST_SYNC_PATH', os.path.join(APP_RUNTIME_DIR, 'trakt_last_sync.json'))
TRAKT_SETTINGS_PATH = os.getenv('TRAKT_SETTINGS_PATH', os.path.join(APP_RUNTIME_DIR, 'trakt_settings.json'))
TRAKT_SYNC_DB_PATH = os.getenv('TRAKT_SYNC_DB_PATH', os.path.join(APP_RUNTIME_DIR, 'trakt_sync.db'))
TRAKT_TOKEN_REFRESH_BUFFER_SECONDS = 300
TRAKT_AUTO_SYNC_ENABLED = _get_env_bool('TRAKT_AUTO_SYNC_ENABLED', True)
TRAKT_AUTO_SYNC_INTERVAL_SECONDS = max(60, _get_env_int('TRAKT_AUTO_SYNC_INTERVAL_SECONDS', 1800))
TRAKT_AUTO_SYNC_WATCHED_THRESHOLD = max(1, min(100, _get_env_int('TRAKT_AUTO_SYNC_WATCHED_THRESHOLD', 90)))
TRAKT_AUTO_SYNC_LIMIT = max(1, _get_env_int('TRAKT_AUTO_SYNC_LIMIT', 1000))
TRAKT_AUTO_SYNC_USER_GUID = os.getenv('TRAKT_AUTO_SYNC_USER_GUID', '').strip()
_trakt_token_lock = threading.Lock()
_trakt_device_lock = threading.Lock()
_trakt_settings_lock = threading.Lock()
_trakt_sync_db_lock = threading.Lock()
_trakt_device_sessions: Dict[str, Dict[str, Any]] = {}
_trakt_auto_sync_thread: Optional[threading.Thread] = None
_trakt_auto_sync_stop_event = threading.Event()


class TraktAuthError(Exception):
    """Trakt 鉴权相关错误。"""


def _is_trakt_configured() -> bool:
    """检查 Trakt 设备授权所需配置是否齐全。"""
    return bool(TRAKT_CLIENT_ID and TRAKT_CLIENT_SECRET)


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    """以原子方式写入 JSON，避免并发时出现半写入文件。"""
    tmp_path = f'{path}.new'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _load_trakt_token_data() -> Dict[str, Any]:
    """读取已保存的 Trakt token。"""
    with _trakt_token_lock:
        if not os.path.exists(TRAKT_TOKEN_PATH):
            return {}
        try:
            with open(TRAKT_TOKEN_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            logger.exception('读取 Trakt token 文件失败')
        return {}


def _save_trakt_token_data(data: Dict[str, Any]) -> None:
    """保存 Trakt token。"""
    with _trakt_token_lock:
        _atomic_write_json(TRAKT_TOKEN_PATH, data)


def _clear_trakt_token_data() -> None:
    """清理已失效的 Trakt token。"""
    with _trakt_token_lock:
        if os.path.exists(TRAKT_TOKEN_PATH):
            os.remove(TRAKT_TOKEN_PATH)


def _load_trakt_last_sync() -> Dict[str, Any]:
    """读取最近一次 Trakt 同步结果。"""
    if not os.path.exists(TRAKT_LAST_SYNC_PATH):
        return {}

    try:
        with open(TRAKT_LAST_SYNC_PATH, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        logger.exception('读取最近一次 Trakt 同步结果失败')
    return {}


def _save_trakt_last_sync(data: Dict[str, Any]) -> None:
    """保存最近一次 Trakt 同步结果。"""
    _atomic_write_json(TRAKT_LAST_SYNC_PATH, data)


def _normalize_trakt_auto_sync_user_guid(value: Any) -> str:
    """规范化自动同步用户配置。"""
    return str(value or '').strip()


def _load_trakt_settings() -> Dict[str, Any]:
    """读取 Trakt 服务端设置。"""
    default_settings = {
        'auto_sync_user_guid': TRAKT_AUTO_SYNC_USER_GUID
    }

    with _trakt_settings_lock:
        if not os.path.exists(TRAKT_SETTINGS_PATH):
            return default_settings
        try:
            with open(TRAKT_SETTINGS_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return default_settings
        except (OSError, json.JSONDecodeError):
            logger.exception('读取 Trakt 设置失败')
            return default_settings

    return {
        'auto_sync_user_guid': _normalize_trakt_auto_sync_user_guid(
            data.get('auto_sync_user_guid', TRAKT_AUTO_SYNC_USER_GUID)
        )
    }


def _save_trakt_settings(data: Dict[str, Any]) -> Dict[str, Any]:
    """保存 Trakt 服务端设置。"""
    normalized = {
        'auto_sync_user_guid': _normalize_trakt_auto_sync_user_guid(
            data.get('auto_sync_user_guid', TRAKT_AUTO_SYNC_USER_GUID)
        )
    }
    with _trakt_settings_lock:
        _atomic_write_json(TRAKT_SETTINGS_PATH, normalized)
    return normalized


def _get_sync_user_info(user_guid: str) -> Optional[Dict[str, Any]]:
    """读取单个可用用户信息。"""
    normalized_guid = _normalize_trakt_auto_sync_user_guid(user_guid)
    if not normalized_guid:
        return None

    try:
        with get_db_connection() as conn:
            row = conn.execute('''
                SELECT guid, username, is_admin
                FROM user
                WHERE guid = ? AND status = 1 AND guid != 'default-user-template'
                LIMIT 1
            ''', (normalized_guid,)).fetchone()
    except sqlite3.Error:
        logger.exception('读取自动同步用户信息失败')
        return None

    return dict(row) if row else None


def _build_trakt_auto_sync_scope() -> Dict[str, str]:
    """构建自动同步范围信息。"""
    settings = _load_trakt_settings()
    user_guid = _normalize_trakt_auto_sync_user_guid(settings.get('auto_sync_user_guid'))
    user_info = _get_sync_user_info(user_guid)
    if not user_guid:
        return {
            'auto_sync_user_guid': '',
            'auto_sync_user_display': '所有用户'
        }
    if user_info:
        return {
            'auto_sync_user_guid': user_guid,
            'auto_sync_user_display': f"{user_info['username']}{' (管理员)' if user_info.get('is_admin') else ''}"
        }
    return {
        'auto_sync_user_guid': user_guid,
        'auto_sync_user_display': f'未知用户 ({user_guid})'
    }


def _get_trakt_sync_db_connection() -> sqlite3.Connection:
    """获取本地 Trakt 同步缓存库连接。"""
    conn = sqlite3.connect(TRAKT_SYNC_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_trakt_sync_tables() -> None:
    """初始化 Trakt 本地缓存表。"""
    with _trakt_sync_db_lock:
        with _get_trakt_sync_db_connection() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS trakt_match_cache (
                    item_guid TEXT PRIMARY KEY,
                    media_type TEXT NOT NULL,
                    trakt_show_id INTEGER,
                    trakt_episode_id INTEGER,
                    trakt_movie_id INTEGER,
                    matched_by TEXT,
                    confidence TEXT,
                    raw_match_snapshot TEXT,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trakt_sync_state (
                    user_guid TEXT NOT NULL,
                    item_guid TEXT NOT NULL,
                    last_synced_watched INTEGER,
                    last_synced_at INTEGER,
                    last_remote_type TEXT,
                    last_remote_id INTEGER,
                    sync_status TEXT,
                    error_message TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (user_guid, item_guid)
                );

                CREATE TABLE IF NOT EXISTS trakt_failed_queue (
                    user_guid TEXT NOT NULL,
                    item_guid TEXT NOT NULL,
                    reason TEXT,
                    candidate_payload TEXT,
                    manual_resolution TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (user_guid, item_guid)
                );
            ''')


def _get_cached_match(item_guid: str) -> Optional[Dict[str, Any]]:
    """读取本地匹配缓存。"""
    _ensure_trakt_sync_tables()
    with _trakt_sync_db_lock:
        with _get_trakt_sync_db_connection() as conn:
            row = conn.execute(
                'SELECT * FROM trakt_match_cache WHERE item_guid = ?',
                (item_guid,)
            ).fetchone()
    return dict(row) if row else None


def _upsert_match_cache(item_guid: str, media_type: str, trakt_show_id: Optional[int], trakt_episode_id: Optional[int], trakt_movie_id: Optional[int], matched_by: str, confidence: str, raw_match_snapshot: Dict[str, Any]) -> None:
    """写入本地匹配缓存。"""
    _ensure_trakt_sync_tables()
    payload = json.dumps(raw_match_snapshot, ensure_ascii=False)
    now_ts = int(time.time())
    with _trakt_sync_db_lock:
        with _get_trakt_sync_db_connection() as conn:
            conn.execute('''
                INSERT INTO trakt_match_cache (
                    item_guid, media_type, trakt_show_id, trakt_episode_id, trakt_movie_id,
                    matched_by, confidence, raw_match_snapshot, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_guid) DO UPDATE SET
                    media_type = excluded.media_type,
                    trakt_show_id = excluded.trakt_show_id,
                    trakt_episode_id = excluded.trakt_episode_id,
                    trakt_movie_id = excluded.trakt_movie_id,
                    matched_by = excluded.matched_by,
                    confidence = excluded.confidence,
                    raw_match_snapshot = excluded.raw_match_snapshot,
                    updated_at = excluded.updated_at
            ''', (
                item_guid, media_type, trakt_show_id, trakt_episode_id, trakt_movie_id,
                matched_by, confidence, payload, now_ts
            ))


def _upsert_sync_state(user_guid: str, item_guid: str, watched: bool, watched_at: Optional[str], remote_type: Optional[str], remote_id: Optional[int], sync_status: str, error_message: str = '', retry_increment: bool = False) -> None:
    """记录同步状态。"""
    _ensure_trakt_sync_tables()
    now_ts = int(time.time())
    with _trakt_sync_db_lock:
        with _get_trakt_sync_db_connection() as conn:
            existing = conn.execute(
                'SELECT retry_count FROM trakt_sync_state WHERE user_guid = ? AND item_guid = ?',
                (user_guid, item_guid)
            ).fetchone()
            retry_count = int(existing['retry_count']) if existing else 0
            if retry_increment:
                retry_count += 1
            conn.execute('''
                INSERT INTO trakt_sync_state (
                    user_guid, item_guid, last_synced_watched, last_synced_at, last_remote_type,
                    last_remote_id, sync_status, error_message, retry_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_guid, item_guid) DO UPDATE SET
                    last_synced_watched = excluded.last_synced_watched,
                    last_synced_at = excluded.last_synced_at,
                    last_remote_type = excluded.last_remote_type,
                    last_remote_id = excluded.last_remote_id,
                    sync_status = excluded.sync_status,
                    error_message = excluded.error_message,
                    retry_count = excluded.retry_count,
                    updated_at = excluded.updated_at
            ''', (
                user_guid, item_guid, 1 if watched else 0, watched_at, remote_type,
                remote_id, sync_status, error_message, retry_count, now_ts
            ))


def _enqueue_failed_item(user_guid: str, item_guid: str, reason: str, candidate_payload: Dict[str, Any]) -> None:
    """将失败项写入待处理队列。"""
    _ensure_trakt_sync_tables()
    now_ts = int(time.time())
    payload = json.dumps(candidate_payload, ensure_ascii=False)
    with _trakt_sync_db_lock:
        with _get_trakt_sync_db_connection() as conn:
            conn.execute('''
                INSERT INTO trakt_failed_queue (
                    user_guid, item_guid, reason, candidate_payload, manual_resolution, status, updated_at
                ) VALUES (?, ?, ?, ?, '', 'pending', ?)
                ON CONFLICT(user_guid, item_guid) DO UPDATE SET
                    reason = excluded.reason,
                    candidate_payload = excluded.candidate_payload,
                    status = 'pending',
                    updated_at = excluded.updated_at
            ''', (
                user_guid, item_guid, reason, payload, now_ts
            ))


def _update_failed_queue_status(user_guid: str, item_guid: str, status: str, manual_resolution: str = '') -> None:
    """更新失败队列状态。"""
    _ensure_trakt_sync_tables()
    now_ts = int(time.time())
    with _trakt_sync_db_lock:
        with _get_trakt_sync_db_connection() as conn:
            conn.execute('''
                UPDATE trakt_failed_queue
                SET status = ?, manual_resolution = ?, updated_at = ?
                WHERE user_guid = ? AND item_guid = ?
            ''', (status, manual_resolution, now_ts, user_guid, item_guid))


def _update_failed_queue_item(user_guid: str, item_guid: str, status: str, candidate_payload: Optional[Dict[str, Any]] = None, manual_resolution: str = '', reason: Optional[str] = None) -> None:
    """更新失败队列状态，并可同步刷新候选快照。"""
    _ensure_trakt_sync_tables()
    now_ts = int(time.time())
    payload = json.dumps(candidate_payload, ensure_ascii=False) if candidate_payload is not None else None
    with _trakt_sync_db_lock:
        with _get_trakt_sync_db_connection() as conn:
            if candidate_payload is None and reason is None:
                conn.execute('''
                    UPDATE trakt_failed_queue
                    SET status = ?, manual_resolution = ?, updated_at = ?
                    WHERE user_guid = ? AND item_guid = ?
                ''', (status, manual_resolution, now_ts, user_guid, item_guid))
            elif candidate_payload is None:
                conn.execute('''
                    UPDATE trakt_failed_queue
                    SET status = ?, reason = ?, manual_resolution = ?, updated_at = ?
                    WHERE user_guid = ? AND item_guid = ?
                ''', (status, reason, manual_resolution, now_ts, user_guid, item_guid))
            elif reason is None:
                conn.execute('''
                    UPDATE trakt_failed_queue
                    SET status = ?, candidate_payload = ?, manual_resolution = ?, updated_at = ?
                    WHERE user_guid = ? AND item_guid = ?
                ''', (status, payload, manual_resolution, now_ts, user_guid, item_guid))
            else:
                conn.execute('''
                    UPDATE trakt_failed_queue
                    SET status = ?, reason = ?, candidate_payload = ?, manual_resolution = ?, updated_at = ?
                    WHERE user_guid = ? AND item_guid = ?
                ''', (status, reason, payload, manual_resolution, now_ts, user_guid, item_guid))


def _load_failed_queue(limit: int = 50, statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """读取失败队列。"""
    _ensure_trakt_sync_tables()
    statuses = [str(status).strip() for status in (statuses or []) if str(status).strip()]
    with _trakt_sync_db_lock:
        with _get_trakt_sync_db_connection() as conn:
            if statuses:
                placeholders = ', '.join('?' for _ in statuses)
                rows = conn.execute(f'''
                    SELECT user_guid, item_guid, reason, candidate_payload, manual_resolution, status, updated_at
                    FROM trakt_failed_queue
                    WHERE status IN ({placeholders})
                    ORDER BY updated_at DESC
                    LIMIT ?
                ''', (*statuses, limit)).fetchall()
            else:
                rows = conn.execute('''
                    SELECT user_guid, item_guid, reason, candidate_payload, manual_resolution, status, updated_at
                    FROM trakt_failed_queue
                    ORDER BY updated_at DESC
                    LIMIT ?
                ''', (limit,)).fetchall()

    result: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key in ('candidate_payload', 'manual_resolution'):
            raw_value = item.get(key) or ''
            if raw_value:
                try:
                    item[key] = json.loads(raw_value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    item[key] = raw_value
            else:
                item[key] = {}
        item['candidate_payload'] = _enrich_failed_queue_candidate_payload(str(item.get('item_guid') or ''), item.get('candidate_payload') or {})
        item['updated_at_display'] = datetime.fromtimestamp(int(row['updated_at'])).strftime('%Y-%m-%d %H:%M:%S')
        result.append(item)
    return result


def _get_failed_queue_item(user_guid: str, item_guid: str) -> Optional[Dict[str, Any]]:
    """读取单条失败队列记录。"""
    _ensure_trakt_sync_tables()
    with _trakt_sync_db_lock:
        with _get_trakt_sync_db_connection() as conn:
            row = conn.execute('''
                SELECT user_guid, item_guid, reason, candidate_payload, manual_resolution, status, updated_at
                FROM trakt_failed_queue
                WHERE user_guid = ? AND item_guid = ?
                LIMIT 1
            ''', (user_guid, item_guid)).fetchone()

    if not row:
        return None

    item = dict(row)
    for key in ('candidate_payload', 'manual_resolution'):
        raw_value = item.get(key) or ''
        if raw_value:
            try:
                item[key] = json.loads(raw_value)
            except (TypeError, ValueError, json.JSONDecodeError):
                item[key] = raw_value
        else:
            item[key] = {}
    item['candidate_payload'] = _enrich_failed_queue_candidate_payload(item_guid, item.get('candidate_payload') or {})
    item['updated_at_display'] = datetime.fromtimestamp(int(row['updated_at'])).strftime('%Y-%m-%d %H:%M:%S')
    return item


def _load_sync_states(limit: int = 50) -> List[Dict[str, Any]]:
    """读取同步状态列表。"""
    _ensure_trakt_sync_tables()
    with _trakt_sync_db_lock:
        with _get_trakt_sync_db_connection() as conn:
            rows = conn.execute('''
                SELECT user_guid, item_guid, last_synced_watched, last_synced_at, last_remote_type,
                       last_remote_id, sync_status, error_message, retry_count, updated_at
                FROM trakt_sync_state
                ORDER BY updated_at DESC
                LIMIT ?
            ''', (limit,)).fetchall()

    source_details: Dict[Tuple[str, str], Dict[str, Any]] = {}
    try:
        with get_db_connection() as src_conn:
            hierarchy_cache: Dict[str, List[Dict[str, Any]]] = {}
            for row in rows:
                key = (str(row['user_guid']), str(row['item_guid']))
                detail = src_conn.execute('''
                    SELECT u.username, i.title, i.season_number, i.episode_number
                    FROM item_user_play iup
                    JOIN user u ON iup.user_guid = u.guid
                    JOIN item i ON iup.item_guid = i.guid
                    WHERE iup.user_guid = ? AND iup.item_guid = ?
                    ORDER BY iup.update_time DESC
                    LIMIT 1
                ''', key).fetchone()
                if detail:
                    detail_dict = dict(detail)
                    is_episode = detail_dict.get('season_number') is not None and detail_dict.get('episode_number') is not None
                    hierarchy = get_item_hierarchy(src_conn, str(row['item_guid']), hierarchy_cache)
                    series_title = ''
                    if len(hierarchy) > 1:
                        root_item = hierarchy[-1]
                        series_title = str(root_item.get('title') or '').strip()
                    detail_dict['series_title'] = series_title
                    detail_dict['is_episode'] = is_episode
                    source_details[key] = detail_dict
    except sqlite3.Error:
        logger.exception('读取同步状态源数据失败')

    result: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item['updated_at_display'] = datetime.fromtimestamp(int(row['updated_at'])).strftime('%Y-%m-%d %H:%M:%S')
        detail = source_details.get((str(row['user_guid']), str(row['item_guid'])), {})
        item['username'] = detail.get('username', '')
        base_title = detail.get('title', '')
        series_title = detail.get('series_title', '')
        if detail.get('season_number') is not None and detail.get('episode_number') is not None:
            if series_title:
                item['title'] = f"{series_title} - S{int(detail['season_number']):02d}E{int(detail['episode_number']):02d} - {base_title}"
            else:
                item['title'] = f"S{int(detail['season_number']):02d}E{int(detail['episode_number']):02d} - {base_title}"
        else:
            item['title'] = f'{series_title} - {base_title}' if series_title and base_title != series_title else base_title
        result.append(item)
    return result


def _get_sync_state(user_guid: str, item_guid: str) -> Optional[Dict[str, Any]]:
    """读取单条同步状态。"""
    _ensure_trakt_sync_tables()
    with _trakt_sync_db_lock:
        with _get_trakt_sync_db_connection() as conn:
            row = conn.execute(
                'SELECT * FROM trakt_sync_state WHERE user_guid = ? AND item_guid = ?',
                (user_guid, item_guid)
            ).fetchone()
    return dict(row) if row else None


def _build_trakt_dashboard(limit: int = 20) -> Dict[str, Any]:
    """构建 Trakt 运营面板数据。"""
    failed_queue = _load_failed_queue(limit, statuses=['pending'])
    all_failed_queue = _load_failed_queue(limit * 5)
    sync_states = _load_sync_states(limit)
    summary = {
        'failed_pending': sum(1 for row in all_failed_queue if row.get('status') == 'pending'),
        'failed_resolved': sum(1 for row in all_failed_queue if row.get('status') == 'resolved'),
        'review_required': sum(1 for row in sync_states if row.get('sync_status') == 'review_required'),
        'matched': sum(1 for row in sync_states if row.get('sync_status') == 'matched'),
        'failed': sum(1 for row in sync_states if row.get('sync_status') == 'failed')
    }
    return {
        'summary': summary,
        'failed_queue': failed_queue,
        'sync_states': sync_states
    }


def _fetch_single_record_for_trakt_sync(conn: sqlite3.Connection, user_guid: str, item_guid: str) -> Optional[sqlite3.Row]:
    """读取单条播放记录用于重新匹配。"""
    return conn.execute('''
        SELECT
            iup.item_guid,
            iup.user_guid,
            iup.update_time,
            iup.watched,
            i.type AS item_type,
            i.title AS item_title,
            i.season_number,
            i.episode_number,
            i.imdb_id AS item_imdb_id,
            i.tmdb_id AS item_tmdb_id,
            NULL AS item_tvdb_id,
            NULL AS item_slug
        FROM item_user_play iup
        JOIN item i ON iup.item_guid = i.guid
        WHERE iup.visible = 1 AND iup.user_guid = ? AND iup.item_guid = ?
        ORDER BY iup.update_time DESC
        LIMIT 1
    ''', (user_guid, item_guid)).fetchone()


def _get_trakt_token_expire_at(token_data: Dict[str, Any]) -> int:
    """根据 created_at 与 expires_in 计算 token 过期时间。"""
    created_at = int(token_data.get('created_at') or 0)
    expires_in = int(token_data.get('expires_in') or 0)
    return created_at + expires_in


def _build_trakt_auth_headers(access_token: Optional[str] = None) -> Dict[str, str]:
    """构建 Trakt API 通用请求头。"""
    headers = {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': TRAKT_CLIENT_ID
    }
    if access_token:
        headers['Authorization'] = f'Bearer {access_token}'
    return headers


def _trakt_post(path: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, Any]]:
    """向 Trakt 发送 JSON POST 请求。"""
    response = requests.post(
        f'{TRAKT_API_BASE}{path}',
        headers=headers or {'Content-Type': 'application/json'},
        json=payload,
        timeout=30
    )
    content_type = response.headers.get('Content-Type', '')
    if 'application/json' in content_type:
        body = response.json()
    else:
        body = {'raw': response.text}
    return response.status_code, body


def _trakt_get(path: str, headers: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
    """向 Trakt 发送 GET 请求。"""
    response = requests.get(
        f'{TRAKT_API_BASE}{path}',
        headers=headers or _build_trakt_auth_headers(),
        params=params,
        timeout=30
    )
    content_type = response.headers.get('Content-Type', '')
    if 'application/json' in content_type:
        body = response.json()
    else:
        body = {'raw': response.text}
    return response.status_code, body


def _store_trakt_token_response(token_data: Dict[str, Any]) -> Dict[str, Any]:
    """保存 Trakt token 响应，并兼容 refresh_token 未返回的情况。"""
    current_data = _load_trakt_token_data()
    merged = dict(current_data)
    merged.update(token_data)
    if not merged.get('refresh_token') and current_data.get('refresh_token'):
        merged['refresh_token'] = current_data['refresh_token']
    merged['saved_at'] = int(time.time())
    _save_trakt_token_data(merged)
    return merged


def _refresh_trakt_access_token(refresh_token: str) -> Dict[str, Any]:
    """使用 refresh_token 自动刷新 access_token。"""
    payload = {
        'refresh_token': refresh_token,
        'client_id': TRAKT_CLIENT_ID,
        'client_secret': TRAKT_CLIENT_SECRET,
        'redirect_uri': TRAKT_REDIRECT_URI,
        'grant_type': 'refresh_token'
    }
    status_code, response_data = _trakt_post(
        '/oauth/token',
        payload,
        headers={'Content-Type': 'application/json'}
    )
    if status_code >= 400:
        raise TraktAuthError(response_data.get('error_description') or response_data.get('error') or '刷新 Trakt token 失败')
    return _store_trakt_token_response(response_data)


def _ensure_trakt_access_token() -> str:
    """确保服务端持有可用的 Trakt access token。"""
    if not _is_trakt_configured():
        raise TraktAuthError('缺少 Trakt 服务端配置，请设置 TRAKT_CLIENT_ID 与 TRAKT_CLIENT_SECRET')

    token_data = _load_trakt_token_data()
    access_token = str(token_data.get('access_token') or '').strip()
    refresh_token = str(token_data.get('refresh_token') or '').strip()
    expire_at = _get_trakt_token_expire_at(token_data)
    now_ts = int(time.time())

    if access_token and expire_at > (now_ts + TRAKT_TOKEN_REFRESH_BUFFER_SECONDS):
        return access_token

    if not refresh_token:
        raise TraktAuthError('Trakt 尚未连接，请先完成设备授权')

    refreshed = _refresh_trakt_access_token(refresh_token)
    access_token = str(refreshed.get('access_token') or '').strip()
    if not access_token:
        raise TraktAuthError('刷新 Trakt token 后未获取到 access_token')
    return access_token


def _build_trakt_status() -> Dict[str, Any]:
    """生成当前 Trakt 连接状态。"""
    token_data = _load_trakt_token_data()
    access_token = str(token_data.get('access_token') or '').strip()
    refresh_token = str(token_data.get('refresh_token') or '').strip()
    expire_at = _get_trakt_token_expire_at(token_data)
    now_ts = int(time.time())
    auto_sync_scope = _build_trakt_auto_sync_scope()

    return {
        'configured': _is_trakt_configured(),
        'connected': bool(access_token and refresh_token),
        'expires_at': expire_at if expire_at else None,
        'expires_at_display': datetime.fromtimestamp(expire_at).strftime('%Y-%m-%d %H:%M:%S') if expire_at else '',
        'is_expired': bool(expire_at and expire_at <= now_ts),
        'has_refresh_token': bool(refresh_token),
        'auto_sync_enabled': TRAKT_AUTO_SYNC_ENABLED,
        'auto_sync_interval_seconds': TRAKT_AUTO_SYNC_INTERVAL_SECONDS,
        'auto_sync_watched_threshold': TRAKT_AUTO_SYNC_WATCHED_THRESHOLD,
        'auto_sync_limit': TRAKT_AUTO_SYNC_LIMIT,
        **auto_sync_scope,
        'last_sync': _load_trakt_last_sync()
    }


def _cleanup_expired_trakt_sessions() -> None:
    """清理过期的设备授权会话。"""
    now_ts = time.time()
    expired_session_ids = [
        session_id
        for session_id, session in _trakt_device_sessions.items()
        if session.get('expires_at', 0) <= now_ts
    ]
    for session_id in expired_session_ids:
        _trakt_device_sessions.pop(session_id, None)


def _start_trakt_device_session() -> Dict[str, Any]:
    """向 Trakt 申请 device code，并保存在服务端会话中。"""
    status_code, response_data = _trakt_post(
        '/oauth/device/code',
        {'client_id': TRAKT_CLIENT_ID},
        headers={'Content-Type': 'application/json'}
    )
    if status_code >= 400:
        raise TraktAuthError(response_data.get('error_description') or response_data.get('error') or '申请 Trakt 设备授权失败')

    session_id = uuid.uuid4().hex
    interval = int(response_data.get('interval') or 5)
    session = {
        'session_id': session_id,
        'device_code': response_data['device_code'],
        'user_code': response_data['user_code'],
        'verification_url': response_data['verification_url'],
        'interval': interval,
        'started_at': time.time(),
        'expires_at': time.time() + int(response_data.get('expires_in') or 0),
        'next_poll_at': time.time() + interval
    }
    with _trakt_device_lock:
        _cleanup_expired_trakt_sessions()
        _trakt_device_sessions[session_id] = session
    return {
        'session_id': session_id,
        'user_code': session['user_code'],
        'verification_url': session['verification_url'],
        'expires_in': int(response_data.get('expires_in') or 0),
        'interval': interval
    }


def _poll_trakt_device_session(session_id: str) -> Tuple[int, Dict[str, Any]]:
    """轮询设备授权状态，授权完成后保存 token。"""
    with _trakt_device_lock:
        _cleanup_expired_trakt_sessions()
        session = _trakt_device_sessions.get(session_id)

    if not session:
        return 404, {'error': '设备授权会话不存在或已过期'}

    now_ts = time.time()
    if session['expires_at'] <= now_ts:
        with _trakt_device_lock:
            _trakt_device_sessions.pop(session_id, None)
        return 400, {'status': 'expired', 'error': '设备授权已过期，请重新发起连接'}

    retry_after = max(0, int(session['next_poll_at'] - now_ts))
    if retry_after > 0:
        return 200, {
            'status': 'pending',
            'retry_after': retry_after,
            'verification_url': session['verification_url'],
            'user_code': session['user_code']
        }

    payload = {
        'code': session['device_code'],
        'client_id': TRAKT_CLIENT_ID,
        'client_secret': TRAKT_CLIENT_SECRET
    }
    status_code, response_data = _trakt_post(
        '/oauth/device/token',
        payload,
        headers={'Content-Type': 'application/json'}
    )

    if status_code < 400:
        saved_token = _store_trakt_token_response(response_data)
        with _trakt_device_lock:
            _trakt_device_sessions.pop(session_id, None)
        return 200, {
            'status': 'authorized',
            'message': 'Trakt 授权成功',
            'connected': True,
            'expires_at': _get_trakt_token_expire_at(saved_token),
            'expires_at_display': datetime.fromtimestamp(_get_trakt_token_expire_at(saved_token)).strftime('%Y-%m-%d %H:%M:%S')
        }

    error_code = response_data.get('error')
    if error_code == 'authorization_pending':
        session['next_poll_at'] = time.time() + session['interval']
        with _trakt_device_lock:
            _trakt_device_sessions[session_id] = session
        return 200, {
            'status': 'pending',
            'retry_after': session['interval'],
            'verification_url': session['verification_url'],
            'user_code': session['user_code']
        }
    if error_code == 'slow_down':
        session['interval'] += 5
        session['next_poll_at'] = time.time() + session['interval']
        with _trakt_device_lock:
            _trakt_device_sessions[session_id] = session
        return 200, {
            'status': 'pending',
            'retry_after': session['interval'],
            'verification_url': session['verification_url'],
            'user_code': session['user_code']
        }
    if error_code in {'expired_token', 'access_denied'}:
        with _trakt_device_lock:
            _trakt_device_sessions.pop(session_id, None)
        return 400, {
            'status': 'expired' if error_code == 'expired_token' else 'denied',
            'error': response_data.get('error_description') or error_code
        }
    return status_code, {
        'status': 'error',
        'error': response_data.get('error_description') or response_data.get('error') or 'Trakt 授权失败'
    }

def get_item_hierarchy(conn, item_guid, cache=None):
    """获取项目的完整层级信息（带缓存）"""
    if cache is None:
        cache = {}
    
    if item_guid in cache:
        return cache[item_guid]
    
    # 一次性查询所有可能的父级项目
    hierarchy_query = '''
        WITH RECURSIVE item_hierarchy(guid, title, original_title, parent_guid, level) AS (
            -- 起始项目
            SELECT guid, title, original_title, parent_guid, 0 as level
            FROM item 
            WHERE guid = ?
            
            UNION ALL
            
            -- 递归查找父级
            SELECT i.guid, i.title, i.original_title, i.parent_guid, ih.level + 1
            FROM item i
            INNER JOIN item_hierarchy ih ON i.guid = ih.parent_guid
            WHERE ih.level < 10 AND i.guid IS NOT NULL  -- 增加递归深度并确保不为NULL
        )
        SELECT * FROM item_hierarchy ORDER BY level ASC
    '''
    
    items = conn.execute(hierarchy_query, (item_guid,)).fetchall()
    
    hierarchy = []
    for item in items:
        hierarchy.append({
            'guid': item['guid'],
            'title': item['title'],
            'original_title': item['original_title'],
            'parent_guid': item['parent_guid'],
            'level': item['level']
        })
    
    # 只缓存当前查询的项目层级信息
    cache[item_guid] = hierarchy
    
    return hierarchy

def format_timestamp(timestamp):
    """将时间戳转换为可读格式"""
    if timestamp:
        return datetime.fromtimestamp(timestamp / 1000).strftime('%Y-%m-%d %H:%M:%S')
    return ''

def format_duration(seconds):
    """将秒数转换为时分秒格式"""
    if not seconds:
        return '00:00:00'
    
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

def _get_table_columns(conn: sqlite3.Connection, table_name: str) -> set:
    """获取 SQLite 表字段集合，用于动态构建查询。"""
    rows = conn.execute(f'PRAGMA table_info({table_name})').fetchall()
    return {row['name'] for row in rows}

def _to_bool(value: Any, default: bool = False) -> bool:
    """将不同类型输入转换为布尔值。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'y', 'on'}
    return default


def _clamp_percentage(value: Any, default: int = 90) -> int:
    """限制百分比阈值在 1-100 之间。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(100, number))

def _calculate_play_progress(position_seconds: Any, runtime_minutes: Any) -> float:
    """根据本地播放位置和片长计算进度百分比。"""
    try:
        runtime_value = float(runtime_minutes or 0)
        position_value = float(position_seconds or 0)
    except (TypeError, ValueError):
        return 0.0

    if runtime_value <= 0 or position_value <= 0:
        return 0.0

    runtime_seconds = runtime_value * 60.0
    if runtime_seconds <= 0:
        return 0.0

    return max(0.0, min(100.0, (position_value / runtime_seconds) * 100.0))


def _derive_watch_state(progress: Any) -> str:
    """按进度推导观看状态。"""
    try:
        progress_value = float(progress or 0)
    except (TypeError, ValueError):
        progress_value = 0.0

    if progress_value >= 100:
        return 'watched'
    if progress_value > 0:
        return 'in_progress'
    return 'unwatched'


def _normalize_imdb_id(value: Any) -> Optional[str]:
    """规范化 imdb id，支持 1234567 或 tt1234567。"""
    if value is None:
        return None
    imdb = str(value).strip()
    if not imdb:
        return None
    if imdb.startswith('tt'):
        return imdb
    if imdb.isdigit():
        return f'tt{imdb}'
    return imdb

def _to_trakt_datetime(ms_timestamp: Any) -> Optional[str]:
    """将毫秒时间戳转换为 Trakt 需要的 UTC ISO8601 时间。"""
    try:
        if ms_timestamp is None:
            return None
        ts = int(ms_timestamp) / 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.isoformat().replace('+00:00', 'Z')
    except (TypeError, ValueError, OSError):
        return None

def _extract_item_ids(record: sqlite3.Row) -> Dict[str, Any]:
    """从查询记录中提取 Trakt 支持的媒体 ID。"""
    ids: Dict[str, Any] = {}
    trakt_id = record['item_trakt_id']
    imdb_id = _normalize_imdb_id(record['item_imdb_id'])
    tmdb_id = record['item_tmdb_id']
    tvdb_id = record['item_tvdb_id']
    slug = record['item_slug']

    if trakt_id:
        ids['trakt'] = int(trakt_id)
    if imdb_id:
        ids['imdb'] = imdb_id
    if tmdb_id:
        ids['tmdb'] = int(tmdb_id)
    if tvdb_id:
        ids['tvdb'] = int(tvdb_id)
    if slug:
        ids['slug'] = str(slug)
    return ids

def _build_trakt_history_payload(records: List[sqlite3.Row]) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, int]]:
    """将数据库播放记录转换为 Trakt /sync/history 所需 payload。"""
    movies: List[Dict[str, Any]] = []
    episodes: List[Dict[str, Any]] = []
    skipped_no_ids = 0
    skipped_no_time = 0

    for row in records:
        watched_at = _to_trakt_datetime(row['update_time'])
        if not watched_at:
            skipped_no_time += 1
            continue

        item_ids = _extract_item_ids(row)
        if not item_ids:
            skipped_no_ids += 1
            continue

        entry = {
            'ids': item_ids,
            'watched_at': watched_at
        }

        is_episode = row['season_number'] is not None and row['episode_number'] is not None
        if is_episode:
            episodes.append(entry)
        else:
            movies.append(entry)

    return (
        {
            'movies': movies,
            'episodes': episodes
        },
        {
            'skipped_no_ids': skipped_no_ids,
            'skipped_no_time': skipped_no_time
        }
    )


def _build_trakt_sync_item_summaries(conn: sqlite3.Connection, records: List[sqlite3.Row], max_items: int = 50) -> List[Dict[str, Any]]:
    """构建用于页面展示的 Trakt 同步条目摘要。"""
    hierarchy_cache: Dict[str, List[Dict[str, Any]]] = {}
    summaries: List[Dict[str, Any]] = []

    for row in records[:max_items]:
        watched_at = _to_trakt_datetime(row['update_time'])
        item_ids = _extract_item_ids(row)
        if not watched_at or not item_ids:
            continue

        hierarchy = get_item_hierarchy(conn, row['item_guid'], hierarchy_cache)
        display_title = row['item_title']
        series_title = ''
        is_episode = row['season_number'] is not None and row['episode_number'] is not None

        if len(hierarchy) > 1:
            root_item = hierarchy[-1]
            series_title = root_item['title']
            if is_episode:
                display_title = f"{series_title} - S{row['season_number']:02d}E{row['episode_number']:02d} - {row['item_title']}"
            elif row['item_title'] != series_title:
                display_title = f'{series_title} - {row["item_title"]}'
        elif is_episode:
            display_title = f"S{row['season_number']:02d}E{row['episode_number']:02d} - {row['item_title']}"

        summaries.append({
            'title': display_title,
            'series_title': series_title,
            'item_type': 'episode' if is_episode else 'movie',
            'season_number': row['season_number'],
            'episode_number': row['episode_number'],
            'watched_at': watched_at,
            'watched_at_display': format_timestamp(row['update_time']),
            'ids': item_ids
        })

    return summaries


def _collect_external_ids(value: Any, found: Dict[str, Any]) -> None:
    """递归提取 external_ids 中的常见外部 ID。"""
    if isinstance(value, list):
        for item in value:
            _collect_external_ids(item, found)
        return

    if not isinstance(value, dict):
        return

    source = str(value.get('source') or '').strip().lower()
    source_id = value.get('id')
    if source and source_id:
        if source in {'imdb', 'tmdb', 'tvdb', 'slug'} and source not in found:
            found[source] = source_id

    key_aliases = {
        'imdb': 'imdb',
        'imdb_id': 'imdb',
        'tmdb': 'tmdb',
        'tmdb_id': 'tmdb',
        'tvdb': 'tvdb',
        'tvdb_id': 'tvdb',
        'slug': 'slug',
        'trakt': 'trakt',
        'trakt_id': 'trakt'
    }
    for key, raw_value in value.items():
        normalized_key = str(key).strip().lower()
        alias = key_aliases.get(normalized_key)
        if alias and raw_value not in (None, '', 0) and alias not in found:
            found[alias] = raw_value
        elif isinstance(raw_value, (dict, list)):
            _collect_external_ids(raw_value, found)


def _parse_external_ids(value: Any) -> Dict[str, Any]:
    """解析 external_ids 字段并归一化常见 ID。"""
    if not value:
        return {}

    data = value
    if isinstance(value, str):
        try:
            data = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    found: Dict[str, Any] = {}
    if isinstance(data, dict):
        _collect_external_ids(data, found)
    elif isinstance(data, list):
        _collect_external_ids(data, found)
    return found


def _extract_external_id(external_ids: Dict[str, Any], key_candidates: List[str]) -> Any:
    """从 external_ids 中提取指定 ID。"""
    for key in key_candidates:
        value = external_ids.get(key)
        if value:
            return value
    return None


def _extract_year(value: Any) -> Optional[int]:
    """从日期字符串中提取年份。"""
    if not value:
        return None
    text = str(value).strip()
    if len(text) >= 4 and text[:4].isdigit():
        return int(text[:4])
    return None


def _normalize_text_for_compare(value: Any) -> str:
    """弱校验时使用的标题归一化。"""
    if not value:
        return ''
    text = ''.join(ch.lower() for ch in str(value) if ch.isalnum())
    return text


def _compare_titles(local_title: str, remote_title: str) -> float:
    """计算标题近似度。"""
    local = _normalize_text_for_compare(local_title)
    remote = _normalize_text_for_compare(remote_title)
    if not local or not remote:
        return 0.0
    if local == remote:
        return 1.0
    if local in remote or remote in local:
        return 0.8
    same_chars = len(set(local) & set(remote))
    return same_chars / max(len(set(local)) or 1, len(set(remote)) or 1)


def _int_or_default(value: Any, default: int = -1) -> int:
    """安全转换整数，保留 0 这类有效值。"""
    if value is None or value == '':
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _extract_remote_title_from_snapshot(item_type: str, snapshot: Dict[str, Any]) -> str:
    """从 Trakt 匹配快照中提取远端标题。"""
    if item_type == 'movie':
        return (((snapshot.get('movie') or {}).get('title')) or ((snapshot.get('title')) or ''))

    episode_title = ((snapshot.get('episode') or {}).get('title')) or ''
    show_title = ((snapshot.get('show') or {}).get('title')) or ''
    if show_title and episode_title:
        return f'{show_title} - {episode_title}'
    return episode_title or show_title


def _load_cached_match_snapshot(cached: Dict[str, Any]) -> Dict[str, Any]:
    """解析缓存中的匹配快照。"""
    raw_snapshot = cached.get('raw_match_snapshot')
    if not raw_snapshot:
        return {}
    try:
        snapshot = json.loads(raw_snapshot) if isinstance(raw_snapshot, str) else raw_snapshot
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return snapshot if isinstance(snapshot, dict) else {}


def _enrich_failed_queue_candidate_payload(item_guid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """为历史失败队列数据补齐候选标题等展示字段。"""
    if not payload:
        return {}

    enriched = dict(payload)
    if enriched.get('candidate_title') and enriched.get('candidate_show_id') not in (None, ''):
        return enriched

    cached = _get_cached_match(item_guid) or {}
    raw_snapshot = cached.get('raw_match_snapshot')
    if not raw_snapshot:
        return enriched

    try:
        snapshot = json.loads(raw_snapshot) if isinstance(raw_snapshot, str) else raw_snapshot
    except (TypeError, ValueError, json.JSONDecodeError):
        snapshot = {}

    if not isinstance(snapshot, dict):
        return enriched

    item_type = str(enriched.get('type') or '').strip().lower()
    if not enriched.get('candidate_title'):
        enriched['candidate_title'] = _extract_remote_title_from_snapshot(item_type, snapshot)

    if item_type == 'episode' and enriched.get('candidate_show_id') in (None, ''):
        show_ids = ((snapshot.get('show') or {}).get('ids') or {})
        trakt_show_id = show_ids.get('trakt')
        if trakt_show_id not in (None, ''):
            enriched['candidate_show_id'] = trakt_show_id

    return enriched


def _validate_episode_candidate(item: Dict[str, Any], show: Dict[str, Any], episode: Dict[str, Any], matched_by: str) -> Tuple[str, List[str]]:
    """对候选剧集做弱校验并分级置信度。"""
    notes: List[str] = []
    confidence = 'high'
    strong_episode_match = False
    strong_show_match_count = 0

    title_score = _compare_titles(item.get('episode_title') or '', episode.get('title') or '')
    if title_score >= 0.95:
        notes.append('标题完全匹配')
    elif title_score >= 0.75:
        confidence = 'medium'
        notes.append('标题近似匹配')
    elif item.get('episode_title') and episode.get('title'):
        confidence = 'low'
        notes.append('标题差异较大')

    episode_ids = episode.get('ids') or {}
    if item.get('episode_imdb_id') and episode_ids.get('imdb'):
        if _normalize_imdb_id(item['episode_imdb_id']) == _normalize_imdb_id(episode_ids.get('imdb')):
            notes.append('episode_imdb_id 一致')
            strong_episode_match = True
        else:
            confidence = 'low'
            notes.append('episode_imdb_id 不一致')

    episode_external_ids = item.get('episode_external_ids') or {}
    for key in ('tmdb', 'tvdb'):
        local_id = episode_external_ids.get(key)
        remote_id = episode_ids.get(key)
        if local_id and remote_id:
            if str(local_id) == str(remote_id):
                notes.append(f'episode {key} 一致')
                strong_episode_match = True
            else:
                confidence = 'low'
                notes.append(f'episode {key} 不一致')

    if matched_by.startswith('show_'):
        if _int_or_default(episode.get('season')) != int(item['season_number']) or _int_or_default(episode.get('number')) != int(item['episode_number']):
            return 'low', ['季集号不一致']
        show_ids = show.get('ids') or {}
        if item.get('show_tmdb_id') and show_ids.get('tmdb'):
            if str(item['show_tmdb_id']) == str(show_ids.get('tmdb')):
                notes.append('show tmdb 一致')
                strong_show_match_count += 1
            else:
                confidence = 'low'
                notes.append('show tmdb 不一致')
        if item.get('show_imdb_id') and show_ids.get('imdb'):
            if _normalize_imdb_id(item['show_imdb_id']) == _normalize_imdb_id(show_ids.get('imdb')):
                notes.append('show imdb 一致')
                strong_show_match_count += 1
            else:
                confidence = 'low'
                notes.append('show imdb 不一致')

    mismatch_notes = [note for note in notes if note.endswith('不一致')]
    if (
        matched_by == 'episode_imdb'
        and 'episode_imdb_id 一致' in notes
    ) or (
        matched_by == 'episode_tmdb'
        and 'episode tmdb 一致' in notes
    ) or (
        matched_by == 'episode_tvdb'
        and 'episode tvdb 一致' in notes
    ):
        confidence = 'high'
        notes.append('episode 主键强匹配，忽略弱特征差异')
        return confidence, notes

    if (
        strong_show_match_count > 0
        and not mismatch_notes
    ):
        confidence = 'high'
        notes.append('show 主键强匹配，标题降权处理')

    return confidence, notes


def _validate_movie_candidate(item: Dict[str, Any], movie: Dict[str, Any], matched_by: str) -> Tuple[str, List[str]]:
    """对候选电影做弱校验并分级置信度。"""
    notes: List[str] = []
    confidence = 'high'
    movie_ids = movie.get('ids') or {}
    strong_movie_match = False

    title_score = max(
        _compare_titles(item.get('title') or '', movie.get('title') or ''),
        _compare_titles(item.get('original_title') or '', movie.get('title') or '')
    )
    if title_score >= 0.95:
        notes.append('标题完全匹配')
    elif title_score >= 0.75:
        confidence = 'medium'
        notes.append('标题近似匹配')
    elif item.get('title') and movie.get('title'):
        confidence = 'low'
        notes.append('标题差异较大')

    if item.get('year') and movie.get('year'):
        if int(item['year']) == int(movie['year']):
            notes.append('年份一致')
        else:
            confidence = 'low'
            notes.append('年份不一致')

    if item.get('tmdb_id') and movie_ids.get('tmdb'):
        if str(item['tmdb_id']) == str(movie_ids.get('tmdb')):
            notes.append('tmdb 一致')
            strong_movie_match = True
        else:
            confidence = 'low'
            notes.append('tmdb 不一致')
    if item.get('imdb_id') and movie_ids.get('imdb'):
        if _normalize_imdb_id(item['imdb_id']) == _normalize_imdb_id(movie_ids.get('imdb')):
            notes.append('imdb 一致')
            strong_movie_match = True
        else:
            confidence = 'low'
            notes.append('imdb 不一致')

    if (
        matched_by == 'tmdb_id'
        and 'tmdb 一致' in notes
    ) or (
        matched_by == 'imdb_id'
        and 'imdb 一致' in notes
    ):
        confidence = 'high'
        notes.append('movie 主键强匹配，忽略弱特征差异')

    return confidence, notes


def _get_item_detail(conn: sqlite3.Connection, item_guid: str, cache: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """读取 item 详情并做缓存。"""
    if item_guid in cache:
        return cache[item_guid]
    row = conn.execute('''
        SELECT guid, title, original_title, parent_guid, type, imdb_id, tmdb_id,
               external_ids, episode_imdb_id, release_date, first_air_date,
               season_number, episode_number
        FROM item
        WHERE guid = ?
    ''', (item_guid,)).fetchone()
    cache[item_guid] = dict(row) if row else {}
    return cache[item_guid]


def _build_normalized_media_items(conn: sqlite3.Connection, records: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    """构建电影/剧集统一规范化对象。"""
    hierarchy_cache: Dict[str, List[Dict[str, Any]]] = {}
    item_detail_cache: Dict[str, Dict[str, Any]] = {}
    normalized_items: List[Dict[str, Any]] = []

    for row in records:
        watched_at = _to_trakt_datetime(row['update_time'])
        if not watched_at:
            continue

        item_detail = _get_item_detail(conn, row['item_guid'], item_detail_cache)
        is_episode = row['season_number'] is not None and row['episode_number'] is not None
        progress = round(_calculate_play_progress(row['position'], row['runtime_minutes']), 1)
        watch_state = _derive_watch_state(progress)
        common = {
            'item_guid': row['item_guid'],
            'user_guid': row['user_guid'],
            'watched': watch_state == 'watched',
            'watch_state': watch_state,
            'progress': progress,
            'watched_at': watched_at,
            'watched_at_display': format_timestamp(row['update_time']),
            'original_title': item_detail.get('original_title') or '',
            'item_external_ids': _parse_external_ids(item_detail.get('external_ids'))
        }

        if not is_episode:
            normalized_items.append({
                **common,
                'type': 'movie',
                'title': row['item_title'],
                'year': _extract_year(item_detail.get('release_date')),
                'imdb_id': _normalize_imdb_id(item_detail.get('imdb_id')),
                'tmdb_id': item_detail.get('tmdb_id')
            })
            continue

        hierarchy = get_item_hierarchy(conn, row['item_guid'], hierarchy_cache)
        show_guid = hierarchy[-1]['guid'] if hierarchy else item_detail.get('parent_guid')
        show_detail = _get_item_detail(conn, show_guid, item_detail_cache) if show_guid else {}
        show_external_ids = _parse_external_ids(show_detail.get('external_ids'))
        normalized_items.append({
            **common,
            'type': 'episode',
            'episode_title': row['item_title'],
            'show_title': show_detail.get('title') or '',
            'season_number': row['season_number'],
            'episode_number': row['episode_number'],
            'episode_imdb_id': _normalize_imdb_id(item_detail.get('episode_imdb_id')),
            'episode_external_ids': common['item_external_ids'],
            'show_imdb_id': _normalize_imdb_id(show_detail.get('imdb_id')),
            'show_tmdb_id': show_detail.get('tmdb_id'),
            'show_external_ids': show_external_ids,
            'parent_guid': item_detail.get('parent_guid'),
            'display_title': f"{show_detail.get('title') or ''} - S{row['season_number']:02d}E{row['episode_number']:02d} - {row['item_title']}"
        })

    return normalized_items


def _extract_search_entity(search_result: Dict[str, Any], media_type: str) -> Dict[str, Any]:
    """从 Trakt 搜索结果中提取媒体实体。"""
    return search_result.get(media_type) or {}


def _search_trakt_by_id(id_type: str, id_value: Any, media_type: str) -> List[Dict[str, Any]]:
    """按外部 ID 搜索 Trakt 媒体。"""
    if not id_value:
        return []
    access_token = _ensure_trakt_access_token()
    status_code, response_data = _trakt_get(
        f'/search/{id_type}/{id_value}',
        headers=_build_trakt_auth_headers(access_token),
        params={'type': media_type}
    )
    if status_code >= 400 or not isinstance(response_data, list):
        return []
    return response_data


def _get_trakt_show_episode(trakt_show_id: int, season_number: int, episode_number: int) -> Optional[Dict[str, Any]]:
    """根据 Trakt show id 与季集号定位具体剧集。"""
    access_token = _ensure_trakt_access_token()
    status_code, response_data = _trakt_get(
        f'/shows/{trakt_show_id}/seasons',
        headers=_build_trakt_auth_headers(access_token),
        params={'extended': 'episodes,full'}
    )
    if status_code >= 400 or not isinstance(response_data, list):
        return None

    for season in response_data:
        if _int_or_default(season.get('number')) != int(season_number):
            continue
        for episode in season.get('episodes', []):
            if _int_or_default(episode.get('number')) == int(episode_number):
                return episode
    return None


def _match_movie_item(item: Dict[str, Any], bypass_cache: bool = False) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str], List[str]]:
    """匹配电影到 Trakt movie id。"""
    cached = None if bypass_cache else _get_cached_match(item['item_guid'])
    if cached and cached.get('trakt_movie_id'):
        matched_by = cached.get('matched_by') or 'cache'
        confidence = cached.get('confidence') or 'high'
        notes = ['命中本地缓存']
        snapshot = _load_cached_match_snapshot(cached)
        movie_snapshot = (snapshot.get('movie') or snapshot) if snapshot else {}
        if isinstance(movie_snapshot, dict) and movie_snapshot.get('ids'):
            confidence, validated_notes = _validate_movie_candidate(item, movie_snapshot, matched_by)
            notes = validated_notes + ['命中本地缓存']
            if confidence != (cached.get('confidence') or 'high'):
                _upsert_match_cache(item['item_guid'], 'movie', None, None, int(cached['trakt_movie_id']), matched_by, confidence, snapshot)
        return ({
            'ids': {'trakt': int(cached['trakt_movie_id'])},
            'watched_at': item['watched_at']
        }, matched_by, confidence, notes)

    search_candidates = [
        ('tmdb', item.get('tmdb_id')),
        ('imdb', item.get('imdb_id'))
    ]
    for id_type, id_value in search_candidates:
        results = _search_trakt_by_id(id_type, id_value, 'movie')
        if not results:
            continue
        movie = _extract_search_entity(results[0], 'movie')
        trakt_id = (((movie or {}).get('ids') or {}).get('trakt'))
        if trakt_id:
            matched_by = f'{id_type}_id'
            confidence, notes = _validate_movie_candidate(item, movie, matched_by)
            _upsert_match_cache(item['item_guid'], 'movie', None, None, int(trakt_id), matched_by, confidence, results[0])
            return ({
                'ids': {'trakt': int(trakt_id)},
                'watched_at': item['watched_at']
            }, matched_by, confidence, notes)

    return None, None, None, ['未命中电影匹配']


def _match_episode_item(item: Dict[str, Any], bypass_cache: bool = False) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str], Optional[int], List[str]]:
    """匹配剧集到 Trakt episode id。"""
    cached = None if bypass_cache else _get_cached_match(item['item_guid'])
    if cached and cached.get('trakt_episode_id'):
        trakt_episode_id = int(cached['trakt_episode_id'])
        matched_by = cached.get('matched_by') or 'cache'
        confidence = cached.get('confidence') or 'high'
        trakt_show_id = int(cached.get('trakt_show_id') or 0) or None
        notes = ['命中本地缓存']
        snapshot = _load_cached_match_snapshot(cached)
        show_snapshot = snapshot.get('show') if snapshot else {}
        episode_snapshot = snapshot.get('episode') if snapshot else {}
        if isinstance(show_snapshot, dict) and isinstance(episode_snapshot, dict) and episode_snapshot.get('ids'):
            confidence, validated_notes = _validate_episode_candidate(item, show_snapshot, episode_snapshot, matched_by)
            notes = validated_notes + ['命中本地缓存']
            if confidence != (cached.get('confidence') or 'high'):
                _upsert_match_cache(item['item_guid'], 'episode', trakt_show_id, trakt_episode_id, None, matched_by, confidence, snapshot)
        return ({
            'ids': {'trakt': trakt_episode_id},
            'watched_at': item['watched_at']
        }, matched_by, confidence, trakt_show_id, notes)

    quick_candidates = [
        ('imdb', item.get('episode_imdb_id')),
        ('tvdb', _extract_external_id(item.get('episode_external_ids', {}), ['tvdb', 'thetvdb'])),
        ('tmdb', _extract_external_id(item.get('episode_external_ids', {}), ['tmdb']))
    ]
    for id_type, id_value in quick_candidates:
        results = _search_trakt_by_id(id_type, id_value, 'episode')
        if not results:
            continue
        hit = results[0]
        episode = hit.get('episode') or {}
        episode_ids = episode.get('ids') or {}
        trakt_episode_id = episode_ids.get('trakt')
        if trakt_episode_id:
            trakt_show_id = (((hit.get('show') or {}).get('ids') or {}).get('trakt'))
            matched_by = f'episode_{id_type}'
            confidence, notes = _validate_episode_candidate(item, hit.get('show') or {}, episode, matched_by)
            _upsert_match_cache(item['item_guid'], 'episode', trakt_show_id, int(trakt_episode_id), None, matched_by, confidence, hit)
            return ({
                'ids': {'trakt': int(trakt_episode_id)},
                'watched_at': item['watched_at']
            }, matched_by, confidence, int(trakt_show_id) if trakt_show_id else None, notes)

    show_candidates = [
        ('tmdb', item.get('show_tmdb_id')),
        ('imdb', item.get('show_imdb_id'))
    ]
    for id_type, id_value in show_candidates:
        results = _search_trakt_by_id(id_type, id_value, 'show')
        if not results:
            continue
        show = _extract_search_entity(results[0], 'show')
        trakt_show_id = (((show or {}).get('ids') or {}).get('trakt'))
        if not trakt_show_id:
            continue
        episode = _get_trakt_show_episode(int(trakt_show_id), int(item['season_number']), int(item['episode_number']))
        if not episode:
            continue
        episode_ids = episode.get('ids') or {}
        trakt_episode_id = episode_ids.get('trakt')
        if not trakt_episode_id:
            continue

        snapshot = {
            'show': show,
            'episode': episode
        }
        matched_by = f'show_{id_type}_season_episode'
        confidence, notes = _validate_episode_candidate(item, show, episode, matched_by)
        _upsert_match_cache(item['item_guid'], 'episode', int(trakt_show_id), int(trakt_episode_id), None, matched_by, confidence, snapshot)
        return ({
            'ids': {'trakt': int(trakt_episode_id)},
            'watched_at': item['watched_at']
        }, matched_by, confidence, int(trakt_show_id), notes)

    return None, None, None, None, ['未命中剧集匹配']


def _match_trakt_items(normalized_items: List[Dict[str, Any]], bypass_cache: bool = False) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, int], List[Dict[str, Any]]]:
    """将规范化对象匹配为 Trakt history payload。"""
    payload = {'movies': [], 'episodes': []}
    stats = {
        'skipped_no_ids': 0,
        'skipped_no_time': 0,
        'skipped_unmatched': 0,
        'skipped_low_confidence': 0,
        'skipped_already_synced': 0
    }
    matched_items: List[Dict[str, Any]] = []

    for item in normalized_items:
        if not item.get('watched_at'):
            stats['skipped_no_time'] += 1
            _enqueue_failed_item(item['user_guid'], item['item_guid'], 'missing_watched_at', item)
            continue

        if item['type'] == 'movie':
            matched_entry, matched_by, confidence, notes = _match_movie_item(item, bypass_cache=bypass_cache)
            remote_type = 'movie'
            remote_id = (((matched_entry or {}).get('ids') or {}).get('trakt'))
        else:
            matched_entry, matched_by, confidence, trakt_show_id, notes = _match_episode_item(item, bypass_cache=bypass_cache)
            remote_type = 'episode'
            remote_id = (((matched_entry or {}).get('ids') or {}).get('trakt'))

        if not matched_entry:
            stats['skipped_unmatched'] += 1
            failed_payload = dict(item)
            failed_payload['match_notes'] = notes
            _enqueue_failed_item(item['user_guid'], item['item_guid'], 'unmatched', failed_payload)
            _upsert_sync_state(item['user_guid'], item['item_guid'], item['watched'], item['watched_at'], remote_type, None, 'failed', 'unmatched', retry_increment=True)
            continue

        existing_state = _get_sync_state(item['user_guid'], item['item_guid'])
        if (
            existing_state
            and existing_state.get('sync_status') == 'synced'
            and str(existing_state.get('last_synced_at') or '') == str(item['watched_at'])
            and str(existing_state.get('last_remote_type') or '') == remote_type
            and str(existing_state.get('last_remote_id') or '') == str(remote_id or '')
        ):
            stats['skipped_already_synced'] += 1
            continue

        if confidence == 'low':
            stats['skipped_low_confidence'] += 1
            remote_title = ''
            cached = _get_cached_match(item['item_guid']) or {}
            try:
                snapshot = json.loads(cached.get('raw_match_snapshot') or '{}') if cached.get('raw_match_snapshot') else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                snapshot = {}
            remote_title = _extract_remote_title_from_snapshot(item['type'], snapshot)
            review_payload = dict(item)
            review_payload.update({
                'matched_by': matched_by,
                'confidence': confidence,
                'candidate_ids': matched_entry.get('ids', {}),
                'candidate_show_id': trakt_show_id if item['type'] == 'episode' else None,
                'candidate_title': remote_title,
                'match_notes': notes
            })
            _enqueue_failed_item(item['user_guid'], item['item_guid'], 'low_confidence', review_payload)
            _upsert_sync_state(item['user_guid'], item['item_guid'], item['watched'], item['watched_at'], remote_type, int(remote_id) if remote_id else None, 'review_required', '; '.join(notes), retry_increment=True)
            matched_items.append({
                'title': item.get('display_title') or item.get('title') or item.get('episode_title') or '',
                'item_type': item['type'],
                'watched_at': item['watched_at'],
                'watched_at_display': item['watched_at_display'],
                'ids': matched_entry.get('ids', {}),
                'user_guid': item['user_guid'],
                'item_guid': item['item_guid'],
                'remote_type': remote_type,
                'remote_id': remote_id,
                'matched_by': matched_by,
                'confidence': confidence,
                'validation_notes': notes,
                'remote_title': remote_title,
                'sync_decision': 'review_required'
            })
            continue

        payload['movies' if item['type'] == 'movie' else 'episodes'].append(matched_entry)
        _upsert_sync_state(item['user_guid'], item['item_guid'], item['watched'], item['watched_at'], remote_type, int(remote_id) if remote_id else None, 'matched', '; '.join(notes))
        remote_title = ''
        cached = _get_cached_match(item['item_guid']) or {}
        try:
            snapshot = json.loads(cached.get('raw_match_snapshot') or '{}') if cached.get('raw_match_snapshot') else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            snapshot = {}
        remote_title = _extract_remote_title_from_snapshot(item['type'], snapshot)
        matched_items.append({
            'title': item.get('display_title') or item.get('title') or item.get('episode_title') or '',
            'item_type': item['type'],
            'watched_at': item['watched_at'],
            'watched_at_display': item['watched_at_display'],
            'ids': matched_entry.get('ids', {}),
            'user_guid': item['user_guid'],
            'item_guid': item['item_guid'],
            'remote_type': remote_type,
            'remote_id': remote_id,
            'matched_by': matched_by,
            'confidence': confidence,
            'validation_notes': notes,
            'remote_title': remote_title,
            'sync_decision': 'auto_synced'
        })

    return payload, stats, matched_items

def _fetch_records_for_trakt_sync(
    conn: sqlite3.Connection,
    user_guid: str,
    limit: int,
    only_watched: bool,
    watched_threshold: int
) -> List[sqlite3.Row]:
    """按筛选条件读取待同步记录，并动态兼容不同字段结构。"""
    item_columns = _get_table_columns(conn, 'item')

    id_column_candidates = {
        'item_trakt_id': ['trakt_id', 'trakt'],
        'item_imdb_id': ['imdb_id', 'imdb'],
        'item_tmdb_id': ['tmdb_id', 'tmdb'],
        'item_tvdb_id': ['tvdb_id', 'tvdb'],
        'item_slug': ['slug']
    }

    select_parts = [
        'iup.item_guid',
        'iup.user_guid',
        'iup.update_time',
        'iup.watched',
        'iup.ts AS position',
        'i.type AS item_type',
        'i.title AS item_title',
        'i.runtime AS runtime_minutes',
        'i.season_number',
        'i.episode_number'
    ]

    for alias, candidates in id_column_candidates.items():
        chosen = None
        for candidate in candidates:
            if candidate in item_columns:
                chosen = candidate
                break
        if chosen:
            select_parts.append(f'i.{chosen} AS {alias}')
        else:
            select_parts.append(f'NULL AS {alias}')

    where_parts = ['iup.visible = 1']
    params: List[Any] = []

    if only_watched:
        where_parts.append('''
            (
                i.runtime IS NOT NULL
                AND i.runtime > 0
                AND iup.ts IS NOT NULL
                AND (CAST(iup.ts AS REAL) * 100.0 / (i.runtime * 60.0)) >= ?
            )
        ''')
        params.append(watched_threshold)
    if user_guid:
        where_parts.append('iup.user_guid = ?')
        params.append(user_guid)

    params.append(limit)

    query = f'''
        SELECT {", ".join(select_parts)}
        FROM item_user_play iup
        JOIN item i ON iup.item_guid = i.guid
        WHERE {" AND ".join(where_parts)}
        ORDER BY iup.update_time DESC
        LIMIT ?
    '''
    return conn.execute(query, params).fetchall()

def _push_to_trakt_history(payload: Dict[str, Any], client_id: str, access_token: str) -> Tuple[int, Dict[str, Any]]:
    """调用 Trakt /sync/history 接口并返回状态码与响应体。"""
    headers = {
        'Content-Type': 'application/json',
        'trakt-api-version': '2',
        'trakt-api-key': client_id,
        'Authorization': f'Bearer {access_token}'
    }
    url = f'{TRAKT_API_BASE}/sync/history'

    response = requests.post(url, headers=headers, json=payload, timeout=30)
    content_type = response.headers.get('Content-Type', '')
    if 'application/json' in content_type:
        response_data = response.json()
    else:
        response_data = {'raw': response.text}
    return response.status_code, response_data


def _mark_items_as_synced(matched_items: List[Dict[str, Any]]) -> None:
    """在成功提交 Trakt 后将条目标记为已同步。"""
    for item in matched_items:
        if item.get('sync_decision') != 'auto_synced':
            continue
        user_guid = str(item.get('user_guid') or '').strip()
        item_guid = str(item.get('item_guid') or '').strip()
        remote_type = str(item.get('remote_type') or '').strip()
        remote_id = item.get('remote_id')
        watched_at = item.get('watched_at')
        if not user_guid or not item_guid or not remote_type or remote_id in (None, ''):
            continue
        _upsert_sync_state(
            user_guid=user_guid,
            item_guid=item_guid,
            watched=True,
            watched_at=watched_at,
            remote_type=remote_type,
            remote_id=int(remote_id),
            sync_status='synced',
            error_message=''
        )


def _sync_failed_queue_candidate(user_guid: str, item_guid: str) -> Tuple[int, Dict[str, Any]]:
    """按失败队列中的候选 Trakt ID 直接同步单条记录。"""
    failed_item = _get_failed_queue_item(user_guid, item_guid)
    if not failed_item:
        return 404, {'error': '未找到对应失败队列记录'}

    candidate_payload = failed_item.get('candidate_payload') or {}
    candidate_ids = candidate_payload.get('candidate_ids') or {}
    trakt_id = candidate_ids.get('trakt')
    media_type = str(candidate_payload.get('type') or '').strip().lower()

    if media_type not in {'movie', 'episode'} or not trakt_id:
        return 400, {'error': '该失败项没有可用的候选匹配项'}

    try:
        trakt_id = int(trakt_id)
    except (TypeError, ValueError):
        return 400, {'error': '候选 Trakt ID 非法'}

    with get_db_connection() as conn:
        row = _fetch_single_record_for_trakt_sync(conn, user_guid, item_guid)
        if not row:
            return 404, {'error': '未找到对应播放记录'}
        normalized_items = _build_normalized_media_items(conn, [row])

    if not normalized_items:
        return 400, {'error': '该播放记录缺少可同步的观看时间或媒体信息'}

    item = normalized_items[0]
    watched_at = item.get('watched_at')
    if not watched_at:
        return 400, {'error': '该播放记录缺少 watched_at，无法同步'}

    payload = {
        'movies': [],
        'episodes': []
    }
    entry = {
        'ids': {'trakt': trakt_id},
        'watched_at': watched_at
    }
    payload['movies' if media_type == 'movie' else 'episodes'].append(entry)

    if not _is_trakt_configured():
        return 400, {'error': '缺少 Trakt 服务端配置，请设置 TRAKT_CLIENT_ID 与 TRAKT_CLIENT_SECRET'}

    try:
        access_token = _ensure_trakt_access_token()
    except TraktAuthError as exc:
        return 400, {'error': str(exc)}

    client_id = TRAKT_CLIENT_ID
    if not client_id or not access_token:
        return 400, {'error': '缺少有效的 Trakt 鉴权信息，请先完成设备授权'}

    try:
        status_code, trakt_response = _push_to_trakt_history(payload, client_id, access_token)
    except requests.RequestException as exc:
        logger.exception('按候选匹配同步 Trakt 失败')
        return 502, {
            'error': '调用 Trakt 接口失败',
            'detail': str(exc)
        }

    if status_code >= 400:
        return status_code, {
            'error': 'Trakt 返回错误',
            'status_code': status_code,
            'trakt_response': trakt_response
        }

    trakt_show_id = candidate_payload.get('candidate_show_id')
    try:
        trakt_show_id = int(trakt_show_id) if trakt_show_id not in (None, '', 0, '0') else None
    except (TypeError, ValueError):
        trakt_show_id = None

    raw_snapshot = {
        'manual': True,
        'source': 'failed_queue_candidate',
        'media_type': media_type,
        'trakt_id': trakt_id,
        'trakt_show_id': trakt_show_id,
        'candidate_title': candidate_payload.get('candidate_title') or '',
        'match_notes': candidate_payload.get('match_notes') or []
    }
    _upsert_match_cache(
        item_guid=item_guid,
        media_type=media_type,
        trakt_show_id=trakt_show_id,
        trakt_episode_id=trakt_id if media_type == 'episode' else None,
        trakt_movie_id=trakt_id if media_type == 'movie' else None,
        matched_by='manual_candidate',
        confidence='high',
        raw_match_snapshot=raw_snapshot
    )
    _upsert_sync_state(
        user_guid=user_guid,
        item_guid=item_guid,
        watched=True,
        watched_at=watched_at,
        remote_type=media_type,
        remote_id=trakt_id,
        sync_status='synced',
        error_message=''
    )
    _update_failed_queue_status(user_guid, item_guid, 'resolved', json.dumps(raw_snapshot, ensure_ascii=False))

    return 200, {
        'message': '已按候选匹配项同步到 Trakt',
        'media_type': media_type,
        'trakt_id': trakt_id,
        'trakt_show_id': trakt_show_id,
        'candidate_title': candidate_payload.get('candidate_title') or '',
        'watched_at': watched_at,
        'trakt_response': trakt_response
    }


def _execute_trakt_sync(user_guid: str, dry_run: bool, only_watched: bool, watched_threshold: int, limit: int, source: str = 'manual') -> Tuple[int, Dict[str, Any]]:
    """执行一次 Trakt 同步。"""
    if limit < 1 or limit > 1000:
        return 400, {'error': 'limit 范围必须在 1-1000 之间'}

    with get_db_connection() as conn:
        rows = _fetch_records_for_trakt_sync(
            conn=conn,
            user_guid=user_guid,
            limit=limit,
            only_watched=only_watched,
            watched_threshold=watched_threshold
        )
        normalized_items = _build_normalized_media_items(conn, rows)

    payload, skipped_stats, matched_items = _match_trakt_items(normalized_items)
    prepared_count = len(payload['movies']) + len(payload['episodes'])

    if prepared_count == 0:
        return 400, {
            'message': '没有可同步的数据（可能没有达到已播放阈值、匹配失败或已同步）',
            'total_records': len(rows),
            'prepared': 0,
            'watched_threshold': watched_threshold,
            'skipped': skipped_stats,
            'items': matched_items,
            'sync_source': source
        }

    if dry_run:
        return 200, {
            'message': 'dry_run 模式，仅生成 payload，未提交 Trakt',
            'total_records': len(rows),
            'prepared': prepared_count,
            'movies': len(payload['movies']),
            'episodes': len(payload['episodes']),
            'watched_threshold': watched_threshold,
            'skipped': skipped_stats,
            'items': matched_items,
            'payload_preview': {
                'movies': payload['movies'][:3],
                'episodes': payload['episodes'][:3]
            },
            'sync_source': source
        }

    if not _is_trakt_configured():
        return 400, {'error': '缺少 Trakt 服务端配置，请设置 TRAKT_CLIENT_ID 与 TRAKT_CLIENT_SECRET'}

    try:
        access_token = _ensure_trakt_access_token()
    except TraktAuthError as exc:
        return 400, {'error': str(exc)}

    client_id = TRAKT_CLIENT_ID
    if not client_id or not access_token:
        return 400, {'error': '缺少有效的 Trakt 鉴权信息，请先完成设备授权'}

    try:
        status_code, trakt_response = _push_to_trakt_history(payload, client_id, access_token)
    except requests.RequestException as exc:
        logger.exception('调用 Trakt 接口失败')
        return 502, {
            'error': '调用 Trakt 接口失败',
            'detail': str(exc)
        }

    if status_code >= 400:
        return status_code, {
            'error': 'Trakt 返回错误',
            'status_code': status_code,
            'trakt_response': trakt_response
        }

    _mark_items_as_synced(matched_items)
    _save_trakt_last_sync({
        'synced_at': int(time.time()),
        'synced_at_display': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'user_guid': user_guid,
        'total_records': len(rows),
        'prepared': prepared_count,
        'movies': len(payload['movies']),
        'episodes': len(payload['episodes']),
        'watched_threshold': watched_threshold,
        'skipped': skipped_stats,
        'trakt_response': trakt_response,
        'items': matched_items,
        'sync_source': source
    })

    return 200, {
        'message': 'Trakt 同步成功',
        'total_records': len(rows),
        'prepared': prepared_count,
        'movies': len(payload['movies']),
        'episodes': len(payload['episodes']),
        'watched_threshold': watched_threshold,
        'skipped': skipped_stats,
        'trakt_response': trakt_response,
        'items': matched_items,
        'sync_source': source
    }


def _trakt_auto_sync_worker() -> None:
    """按固定间隔执行 Trakt 自动同步。"""
    auto_sync_scope = _build_trakt_auto_sync_scope()
    logger.info(
        'Trakt 自动同步线程已启动：间隔=%s秒，阈值=%s%%，limit=%s，范围=%s',
        TRAKT_AUTO_SYNC_INTERVAL_SECONDS,
        TRAKT_AUTO_SYNC_WATCHED_THRESHOLD,
        TRAKT_AUTO_SYNC_LIMIT,
        auto_sync_scope['auto_sync_user_display']
    )

    while not _trakt_auto_sync_stop_event.wait(TRAKT_AUTO_SYNC_INTERVAL_SECONDS):
        try:
            auto_sync_scope = _build_trakt_auto_sync_scope()
            status_code, payload = _execute_trakt_sync(
                user_guid=auto_sync_scope['auto_sync_user_guid'],
                dry_run=False,
                only_watched=True,
                watched_threshold=TRAKT_AUTO_SYNC_WATCHED_THRESHOLD,
                limit=TRAKT_AUTO_SYNC_LIMIT,
                source='auto'
            )
            if status_code >= 400:
                if payload.get('prepared') == 0:
                    logger.info(
                        'Trakt 自动同步本轮无可同步数据：范围=%s, message=%s',
                        auto_sync_scope['auto_sync_user_display'],
                        payload.get('message')
                    )
                else:
                    logger.warning(
                        'Trakt 自动同步未成功：范围=%s, status=%s, message=%s, error=%s',
                        auto_sync_scope['auto_sync_user_display'],
                        status_code,
                        payload.get('message'),
                        payload.get('error')
                    )
            else:
                logger.info(
                    'Trakt 自动同步完成：范围=%s, prepared=%s, movies=%s, episodes=%s',
                    auto_sync_scope['auto_sync_user_display'],
                    payload.get('prepared'),
                    payload.get('movies'),
                    payload.get('episodes')
                )
        except Exception:
            logger.exception('Trakt 自动同步线程执行异常')

    logger.info('Trakt 自动同步线程已停止')


def _start_trakt_auto_sync_thread() -> None:
    """启动 Trakt 自动同步线程。"""
    global _trakt_auto_sync_thread

    if not TRAKT_AUTO_SYNC_ENABLED:
        logger.info('Trakt 自动同步已禁用')
        return

    if _trakt_auto_sync_thread and _trakt_auto_sync_thread.is_alive():
        return

    _trakt_auto_sync_stop_event.clear()
    _trakt_auto_sync_thread = threading.Thread(
        target=_trakt_auto_sync_worker,
        name='TraktAutoSync',
        daemon=True
    )
    _trakt_auto_sync_thread.start()


def _stop_trakt_auto_sync_thread() -> None:
    """停止 Trakt 自动同步线程。"""
    global _trakt_auto_sync_thread

    if not _trakt_auto_sync_thread:
        return

    _trakt_auto_sync_stop_event.set()
    if _trakt_auto_sync_thread.is_alive():
        _trakt_auto_sync_thread.join(timeout=2)
    _trakt_auto_sync_thread = None


atexit.register(_stop_trakt_auto_sync_thread)

@app.route('/')
def index():
    """主页面"""
    return render_template('index.html')

@app.route('/api/meta')
def get_app_meta():
    """获取前端展示用的应用元信息。"""
    return jsonify({
        'version': APP_VERSION,
        'commit_sha': APP_COMMIT_SHA,
        'commit_short': APP_COMMIT_SHA[:7] if APP_COMMIT_SHA and APP_COMMIT_SHA != 'unknown' else 'unknown',
        'build_time': APP_BUILD_TIME
    })

@app.route('/api/db/refresh', methods=['POST'])
def refresh_source_database():
    """手动刷新数据库副本。"""
    payload = _refresh_database_copy(force=True)
    payload['message'] = '数据库副本已刷新'
    return jsonify(payload)

@app.route('/api/users')
def get_users():
    """获取所有用户列表"""
    with get_db_connection() as conn:
        users = conn.execute('''
            SELECT guid, username, last_login_time, is_admin, status
            FROM user 
            WHERE status = 1 AND guid != 'default-user-template'
            ORDER BY username
        ''').fetchall()
        
        user_list = []
        for user in users:
            user_list.append({
                'guid': user['guid'],
                'username': user['username'],
                'last_login': format_timestamp(user['last_login_time']),
                'is_admin': user['is_admin'],
                'status': user['status']
            })
        
        return jsonify(user_list)

@app.route('/api/play_history')
def get_play_history():
    """获取播放历史记录。"""
    user_guid = request.args.get('user_guid', '')
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    search_title = request.args.get('search_title', '').strip()
    start_time = request.args.get('start_time', '')
    end_time = request.args.get('end_time', '')

    with get_db_connection() as conn:
        where_clause = "WHERE iup.visible = 1"
        params: List[Any] = []

        if user_guid:
            where_clause += " AND iup.user_guid = ?"
            params.append(user_guid)

        if search_title:
            logger.info(f"搜索关键词: {search_title}")
            where_clause += """
                AND EXISTS (
                    WITH RECURSIVE item_hierarchy(guid, title, original_title, parent_guid, level) AS (
                        SELECT guid, title, original_title, parent_guid, 0 as level
                        FROM item
                        WHERE guid = i.guid

                        UNION ALL

                        SELECT parent.guid, parent.title, parent.original_title, parent.parent_guid, ih.level + 1
                        FROM item parent
                        INNER JOIN item_hierarchy ih ON parent.guid = ih.parent_guid
                        WHERE ih.level < 10 AND parent.guid IS NOT NULL
                    )
                    SELECT 1 FROM item_hierarchy
                    WHERE title LIKE ? OR original_title LIKE ?
                )
            """
            search_param = f"%{search_title}%"
            params.extend([search_param, search_param])
            logger.debug(f"搜索参数: {search_param}")

        if start_time:
            try:
                start_timestamp = int(datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S').timestamp() * 1000)
                where_clause += " AND iup.update_time >= ?"
                params.append(start_timestamp)
            except ValueError:
                logger.warning(f"无效的开始时间格式: {start_time}")

        if end_time:
            try:
                end_timestamp = int(datetime.strptime(end_time, '%Y-%m-%d %H:%M:%S').timestamp() * 1000)
                where_clause += " AND iup.update_time <= ?"
                params.append(end_timestamp)
            except ValueError:
                logger.warning(f"无效的结束时间格式: {end_time}")

        count_query = f'''
            SELECT COUNT(*) as total
            FROM item_user_play iup
            JOIN user u ON iup.user_guid = u.guid
            JOIN item i ON iup.item_guid = i.guid
            {where_clause}
        '''
        total = conn.execute(count_query, params).fetchone()['total']

        offset = (page - 1) * per_page
        query = f'''
            SELECT
                iup.item_guid,
                iup.user_guid,
                iup.ts AS position,
                iup.watched,
                iup.create_time,
                iup.update_time,
                iup.type AS play_type,
                iup.resolution,
                u.username,
                i.title,
                i.original_title,
                i.overview,
                i.type AS item_type,
                i.season_number,
                i.episode_number,
                i.parent_guid,
                i.runtime,
                i.release_date
            FROM item_user_play iup
            JOIN user u ON iup.user_guid = u.guid
            JOIN item i ON iup.item_guid = i.guid
            {where_clause}
            ORDER BY iup.update_time DESC
            LIMIT ? OFFSET ?
        '''

        query_params = params + [per_page, offset]
        history = conn.execute(query, query_params).fetchall()

        hierarchy_cache: Dict[str, List[Dict[str, Any]]] = {}
        history_list: List[Dict[str, Any]] = []

        for record in history:
            hierarchy = get_item_hierarchy(conn, record['item_guid'], hierarchy_cache)
            display_title = record['title']
            series_info = ''

            if len(hierarchy) > 1:
                root_item = hierarchy[-1]
                series_info = root_item['title']
                if record['season_number'] and record['episode_number']:
                    display_title = f"{series_info} - S{record['season_number']:02d}E{record['episode_number']:02d} - {record['title']}"
                elif record['title'] != series_info:
                    display_title = f"{series_info} - {record['title']}"
            elif record['season_number'] and record['episode_number']:
                display_title = f"S{record['season_number']:02d}E{record['episode_number']:02d} - {record['title']}"

            runtime_seconds = int((record['runtime'] or 0) * 60) if record['runtime'] else 0
            progress = round(_calculate_play_progress(record['position'], record['runtime']), 1)
            watch_state = _derive_watch_state(progress)
            is_episode = record['season_number'] is not None and record['episode_number'] is not None

            history_list.append({
                'item_guid': record['item_guid'],
                'user_guid': record['user_guid'],
                'username': record['username'],
                'title': display_title,
                'original_title': record['original_title'],
                'overview': record['overview'],
                'type': record['item_type'],
                'play_type': record['play_type'],
                'season_number': record['season_number'],
                'episode_number': record['episode_number'],
                'series_title': series_info,
                'hierarchy': [{'title': h['title'], 'level': h['level']} for h in hierarchy],
                'position': record['position'],
                'position_formatted': format_duration(record['position']),
                'runtime': runtime_seconds,
                'runtime_formatted': format_duration(runtime_seconds),
                'progress': progress,
                'watched': watch_state == 'watched',
                'watch_state': watch_state,
                'resolution': record['resolution'],
                'create_time': format_timestamp(record['create_time']),
                'update_time': format_timestamp(record['update_time']),
                'release_date': record['release_date'],
                'is_episode': is_episode,
                'update_time_display': format_timestamp(record['update_time']),
                'play_progress': progress,
                'app_version': record['resolution']
            })

        return jsonify({
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': (total + per_page - 1) // per_page,
            'data': history_list
        })
@app.route('/api/stats')
def get_stats():
    """获取统计数据"""
    with get_db_connection() as conn:
        # 总用户数
        total_users = conn.execute('''
            SELECT COUNT(*) as count 
            FROM user 
            WHERE status = 1 AND guid != 'default-user-template'
        ''').fetchone()['count']
        
        # 总播放记录数（只统计能正确JOIN到item和user表的记录）
        total_plays = conn.execute('''
            SELECT COUNT(*) as count 
            FROM item_user_play iup
            JOIN user u ON iup.user_guid = u.guid
            JOIN item i ON iup.item_guid = i.guid
            WHERE iup.visible = 1
        ''').fetchone()['count']
        
        # 活跃用户数（有播放记录的用户）
        active_users = conn.execute('''
            SELECT COUNT(DISTINCT user_guid) as count 
            FROM item_user_play 
            WHERE visible = 1
        ''').fetchone()['count']
        
        # 最新播放时间
        latest_play = conn.execute('''
            SELECT MAX(update_time) as latest 
            FROM item_user_play 
            WHERE visible = 1
        ''').fetchone()['latest']
        
        # 今日播放数（只统计能正确JOIN到item和user表的记录）
        today_start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        today_plays = conn.execute('''
            SELECT COUNT(*) as count 
            FROM item_user_play iup
            JOIN user u ON iup.user_guid = u.guid
            JOIN item i ON iup.item_guid = i.guid
            WHERE iup.visible = 1 AND iup.update_time >= ?
        ''', (today_start,)).fetchone()['count']
        
        return jsonify({
            'total_users': total_users,
            'total_plays': total_plays,
            'active_users': active_users,
            'today_plays': today_plays,
            'latest_play': format_timestamp(latest_play)
        })

@app.route('/api/user_activity')
def get_user_activity():
    """获取用户活动数据"""
    with get_db_connection() as conn:
        # 用户播放次数统计
        user_stats = conn.execute('''
            SELECT 
                u.username,
                u.guid,
                COUNT(iup.item_guid) as play_count,
                SUM(iup.ts) as total_seconds,
                MAX(iup.update_time) as last_play
            FROM user u
            LEFT JOIN item_user_play iup ON u.guid = iup.user_guid AND iup.visible = 1
            WHERE u.status = 1 AND u.guid != 'default-user-template'
            GROUP BY u.guid, u.username
            ORDER BY play_count DESC
            LIMIT 10
        ''').fetchall()
        
        activity_data = []
        for user in user_stats:
            activity_data.append({
                'username': user['username'],
                'play_count': user['play_count'] or 0,
                'total_hours': round((user['total_seconds'] or 0) / 3600, 1),
                'last_play': format_timestamp(user['last_play'])
            })
        
        return jsonify(activity_data)


@app.route('/api/trakt/status')
def get_trakt_status():
    """获取当前 Trakt 配置与连接状态。"""
    return jsonify(_build_trakt_status())


@app.route('/api/trakt/settings', methods=['POST'])
def save_trakt_settings():
    """保存 Trakt 服务端设置。"""
    data = request.get_json(silent=True) or {}
    auto_sync_user_guid = _normalize_trakt_auto_sync_user_guid(data.get('auto_sync_user_guid'))

    if auto_sync_user_guid and not _get_sync_user_info(auto_sync_user_guid):
        return jsonify({'error': '自动同步用户不存在或已停用'}), 400

    _save_trakt_settings({
        'auto_sync_user_guid': auto_sync_user_guid
    })
    return jsonify({
        'message': '定时同步用户已保存',
        'settings': _build_trakt_auto_sync_scope()
    })


@app.route('/api/trakt/dashboard')
def get_trakt_dashboard():
    """获取 Trakt 运营面板数据。"""
    limit = request.args.get('limit', 20)
    try:
        limit = max(1, min(100, int(limit)))
    except (TypeError, ValueError):
        limit = 20
    return jsonify(_build_trakt_dashboard(limit))


@app.route('/api/trakt/failed_queue/rematch', methods=['POST'])
def rematch_failed_item():
    """重新匹配单条失败项。"""
    data = request.get_json(silent=True) or {}
    user_guid = str(data.get('user_guid') or '').strip()
    item_guid = str(data.get('item_guid') or '').strip()
    if not user_guid or not item_guid:
        return jsonify({'error': '缺少 user_guid 或 item_guid'}), 400

    with get_db_connection() as conn:
        row = _fetch_single_record_for_trakt_sync(conn, user_guid, item_guid)
        if not row:
            return jsonify({'error': '未找到对应播放记录'}), 404
        normalized_items = _build_normalized_media_items(conn, [row])

    failed_item = _get_failed_queue_item(user_guid, item_guid) or {}
    bypass_cache = str(failed_item.get('reason') or '').strip() == 'low_confidence'
    payload, stats, matched_items = _match_trakt_items(normalized_items, bypass_cache=bypass_cache)
    prepared = len(payload['movies']) + len(payload['episodes'])
    if prepared > 0:
        refreshed_payload = dict(normalized_items[0]) if normalized_items else {}
        matched_item = matched_items[0] if matched_items else {}
        refreshed_payload.update({
            'matched_by': matched_item.get('matched_by') or '',
            'confidence': matched_item.get('confidence') or '',
            'candidate_ids': matched_item.get('ids') or {},
            'candidate_show_id': None,
            'candidate_title': matched_item.get('remote_title') or '',
            'match_notes': matched_item.get('validation_notes') or []
        })
        if matched_item.get('item_type') == 'episode' and refreshed_payload.get('candidate_show_id') is None:
            cached = _get_cached_match(item_guid) or {}
            trakt_show_id = cached.get('trakt_show_id')
            if trakt_show_id not in (None, '', 0, '0'):
                refreshed_payload['candidate_show_id'] = int(trakt_show_id)
        _update_failed_queue_item(user_guid, item_guid, 'rematched', candidate_payload=refreshed_payload)
        return jsonify({
            'message': '重新匹配成功',
            'prepared': prepared,
            'items': matched_items,
            'skipped': stats,
            'bypass_cache': bypass_cache
        })

    if int(stats.get('skipped_already_synced') or 0) > 0:
        resolution_payload = {
            'resolved_by': 'already_synced',
            'message': '该记录已同步，无需继续保留在失败队列'
        }
        _update_failed_queue_item(
            user_guid,
            item_guid,
            'resolved',
            candidate_payload=dict(normalized_items[0]) if normalized_items else None,
            manual_resolution=json.dumps(resolution_payload, ensure_ascii=False),
            reason='unmatched'
        )
        return jsonify({
            'message': '该记录已同步，已从失败队列移除',
            'prepared': 0,
            'items': matched_items,
            'skipped': stats,
            'bypass_cache': bypass_cache
        })

    refreshed_payload = dict(normalized_items[0]) if normalized_items else {}
    matched_item = matched_items[0] if matched_items else {}
    refreshed_payload.update({
        'matched_by': matched_item.get('matched_by') or '',
        'confidence': matched_item.get('confidence') or '',
        'candidate_ids': matched_item.get('ids') or {},
        'candidate_show_id': None,
        'candidate_title': matched_item.get('remote_title') or '',
        'match_notes': matched_item.get('validation_notes') or []
    })
    refreshed_reason = 'low_confidence' if matched_item else 'unmatched'
    if matched_item.get('item_type') == 'episode':
        cached = _get_cached_match(item_guid) or {}
        trakt_show_id = cached.get('trakt_show_id')
        if trakt_show_id not in (None, '', 0, '0'):
            refreshed_payload['candidate_show_id'] = int(trakt_show_id)
    _update_failed_queue_item(
        user_guid,
        item_guid,
        'pending',
        candidate_payload=refreshed_payload,
        reason=refreshed_reason
    )
    return jsonify({
        'message': '重新匹配后仍需人工处理',
        'prepared': 0,
        'items': matched_items,
        'skipped': stats,
        'bypass_cache': bypass_cache
    }), 400


@app.route('/api/trakt/failed_queue/manual', methods=['POST'])
def manual_resolve_failed_item():
    """人工指定 Trakt ID 解决失败项。"""
    data = request.get_json(silent=True) or {}
    user_guid = str(data.get('user_guid') or '').strip()
    item_guid = str(data.get('item_guid') or '').strip()
    media_type = str(data.get('media_type') or '').strip().lower()
    trakt_id = data.get('trakt_id')
    trakt_show_id = data.get('trakt_show_id')

    if not user_guid or not item_guid or media_type not in {'movie', 'episode'} or not trakt_id:
        return jsonify({'error': '参数不完整，需提供 user_guid、item_guid、media_type、trakt_id'}), 400

    try:
        trakt_id = int(trakt_id)
        trakt_show_id = int(trakt_show_id) if trakt_show_id not in (None, '', 0, '0') else None
    except (TypeError, ValueError):
        return jsonify({'error': 'trakt_id 或 trakt_show_id 不是有效整数'}), 400

    raw_snapshot = {
        'manual': True,
        'media_type': media_type,
        'trakt_id': trakt_id,
        'trakt_show_id': trakt_show_id
    }
    _upsert_match_cache(
        item_guid=item_guid,
        media_type=media_type,
        trakt_show_id=trakt_show_id,
        trakt_episode_id=trakt_id if media_type == 'episode' else None,
        trakt_movie_id=trakt_id if media_type == 'movie' else None,
        matched_by='manual',
        confidence='high',
        raw_match_snapshot=raw_snapshot
    )
    _upsert_sync_state(user_guid, item_guid, True, None, media_type, trakt_id, 'manual_resolved', '')
    _update_failed_queue_status(user_guid, item_guid, 'resolved', json.dumps(raw_snapshot, ensure_ascii=False))
    return jsonify({
        'message': '人工绑定成功，后续同步将优先使用该 Trakt ID',
        'media_type': media_type,
        'trakt_id': trakt_id,
        'trakt_show_id': trakt_show_id
    })


@app.route('/api/trakt/failed_queue/sync_candidate', methods=['POST'])
def sync_failed_queue_candidate():
    """按失败队列候选匹配项直接同步单条记录。"""
    data = request.get_json(silent=True) or {}
    user_guid = str(data.get('user_guid') or '').strip()
    item_guid = str(data.get('item_guid') or '').strip()
    if not user_guid or not item_guid:
        return jsonify({'error': '缺少 user_guid 或 item_guid'}), 400

    status_code, payload = _sync_failed_queue_candidate(user_guid, item_guid)
    return jsonify(payload), status_code


@app.route('/api/trakt/device/start', methods=['POST'])
def start_trakt_device_auth():
    """发起 Trakt 设备授权流程。"""
    if not _is_trakt_configured():
        return jsonify({
            'error': '缺少 Trakt 服务端配置，请设置 TRAKT_CLIENT_ID 与 TRAKT_CLIENT_SECRET'
        }), 400

    try:
        session_data = _start_trakt_device_session()
    except (requests.RequestException, KeyError) as exc:
        logger.exception('发起 Trakt 设备授权失败')
        return jsonify({
            'error': '发起 Trakt 设备授权失败',
            'detail': str(exc)
        }), 502
    except TraktAuthError as exc:
        return jsonify({'error': str(exc)}), 400

    return jsonify(session_data)


@app.route('/api/trakt/device/status')
def get_trakt_device_auth_status():
    """轮询 Trakt 设备授权状态。"""
    session_id = str(request.args.get('session_id', '')).strip()
    if not session_id:
        return jsonify({'error': '缺少 session_id'}), 400

    if not _is_trakt_configured():
        return jsonify({
            'error': '缺少 Trakt 服务端配置，请设置 TRAKT_CLIENT_ID 与 TRAKT_CLIENT_SECRET'
        }), 400

    try:
        status_code, payload = _poll_trakt_device_session(session_id)
    except requests.RequestException as exc:
        logger.exception('轮询 Trakt 设备授权失败')
        return jsonify({
            'status': 'error',
            'error': '轮询 Trakt 设备授权失败',
            'detail': str(exc)
        }), 502
    except TraktAuthError as exc:
        return jsonify({'status': 'error', 'error': str(exc)}), 400

    return jsonify(payload), status_code

@app.route('/api/trakt/sync', methods=['POST'])
def sync_trakt_history():
    """同步播放历史到 Trakt。"""
    data = request.get_json(silent=True) or {}

    user_guid = str(data.get('user_guid', '')).strip()
    dry_run = _to_bool(data.get('dry_run'), default=False)
    only_watched = _to_bool(data.get('only_watched'), default=True)
    watched_threshold = _clamp_percentage(data.get('watched_threshold', 90), default=90)
    limit = data.get('limit', 200)

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return jsonify({'error': 'limit 必须是整数'}), 400

    status_code, payload = _execute_trakt_sync(
        user_guid=user_guid,
        dry_run=dry_run,
        only_watched=only_watched,
        watched_threshold=watched_threshold,
        limit=limit,
        source='manual'
    )
    return jsonify(payload), status_code

if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("启动飞牛影视观看历史管理系统")
    logger.info("=" * 50)
    logger.info("访问地址: http://127.0.0.1:5000")
    logger.info("Flask 运行模式: 串行处理 (单线程)")
    
    logger.info("所有组件已启动，服务运行中...")
    logger.info("=" * 50)

    _start_trakt_auto_sync_thread()
    app.run(host='0.0.0.0', port=5000)

