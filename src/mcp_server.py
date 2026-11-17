#!/usr/bin/env python3
"""
e-Gov Law MCP Server v2 - Ultra Smart & Efficient

A highly optimized Model Context Protocol server for Japanese e-Gov Law API.
Drastically simplified from 1000+ lines to <500 lines while adding more functionality.

Key Improvements:
- Direct mapping for 16+ major laws (六法 + key legislation)
- Smart Base64/XML text extraction
- Efficient article search with intelligent pattern matching
- Minimal API calls with maximum accuracy
- Clean, maintainable code architecture
"""

import argparse
import base64
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional, TypedDict

import httpx
import yaml
from fastmcp import FastMCP, Context
from fastmcp.exceptions import ToolError, ResourceError

# Optional import for performance monitoring
try:
    import psutil
    PERFORMANCE_MONITORING_AVAILABLE = True
except ImportError:
    PERFORMANCE_MONITORING_AVAILABLE = False

try:
    from .prompt_loader import PromptLoader
except ImportError:
    from prompt_loader import PromptLoader

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Log psutil availability
if not PERFORMANCE_MONITORING_AVAILABLE:
    logger.warning("psutil not available - memory monitoring disabled. Install with: pip install psutil")

# Performance optimization classes
class LRUCache:
    """Thread-safe LRU cache implementation with TTL support"""

    def __init__(self, max_size: int = 100, ttl: int = 3600):
        self.max_size = max_size
        self.ttl = ttl
        self.cache = OrderedDict()
        self.timestamps = {}
        self.lock = threading.RLock()

    def get(self, key: str) -> Optional[Any]:
        with self.lock:
            if key not in self.cache:
                return None

            # Check TTL
            if time.time() - self.timestamps[key] > self.ttl:
                del self.cache[key]
                del self.timestamps[key]
                return None

            # Move to end (most recently used)
            self.cache.move_to_end(key)
            return self.cache[key]

    def put(self, key: str, value: Any) -> None:
        with self.lock:
            if key in self.cache:
                # Update existing key
                self.cache[key] = value
                self.timestamps[key] = time.time()
                self.cache.move_to_end(key)
            else:
                # Add new key
                if len(self.cache) >= self.max_size:
                    # Remove least recently used
                    oldest_key = next(iter(self.cache))
                    del self.cache[oldest_key]
                    del self.timestamps[oldest_key]

                self.cache[key] = value
                self.timestamps[key] = time.time()

    def clear(self) -> None:
        with self.lock:
            self.cache.clear()
            self.timestamps.clear()

    def size(self) -> int:
        with self.lock:
            return len(self.cache)

    def cleanup_expired(self) -> None:
        """Remove expired entries"""
        with self.lock:
            current_time = time.time()
            expired_keys = [
                key for key, timestamp in self.timestamps.items()
                if current_time - timestamp > self.ttl
            ]
            for key in expired_keys:
                del self.cache[key]
                del self.timestamps[key]

class MemoryMonitor:
    """Memory usage monitoring for cache management"""

    def __init__(self, max_memory_mb: int = 512):
        self.max_memory_mb = max_memory_mb
        if PERFORMANCE_MONITORING_AVAILABLE:
            self.process = psutil.Process()
        else:
            self.process = None

    def get_memory_usage_mb(self) -> float:
        """Get current memory usage in MB"""
        if PERFORMANCE_MONITORING_AVAILABLE and self.process:
            return self.process.memory_info().rss / 1024 / 1024
        return 0.0  # Return 0 if psutil not available

    def is_memory_limit_exceeded(self) -> bool:
        """Check if memory limit is exceeded"""
        if not PERFORMANCE_MONITORING_AVAILABLE:
            return False  # Never exceed limit if monitoring disabled
        return self.get_memory_usage_mb() > self.max_memory_mb

class CacheManager:
    """Centralized cache management with prefetching and batch operations"""

    def __init__(self):
        self.law_lookup_cache = LRUCache(max_size=200, ttl=7200)  # 2 hours
        self.law_content_cache = LRUCache(max_size=50, ttl=3600)  # 1 hour
        self.article_cache = LRUCache(max_size=500, ttl=1800)     # 30 minutes
        self.memory_monitor = MemoryMonitor()
        self.batch_pending = {}
        self.batch_lock = threading.Lock()

        # Common law articles for prefetching
        self.common_articles = [
            ("民法", "1"), ("民法", "192"), ("民法", "709"),
            ("憲法", "9"), ("憲法", "14"), ("憲法", "25"),
            ("会社法", "1"), ("会社法", "105"), ("会社法", "362"),
            ("刑法", "1"), ("刑法", "199"), ("刑法", "235"),
        ]

    def get_cache_key(self, law_name: str, article_number: str = None) -> str:
        """Generate cache key"""
        if article_number:
            return f"{law_name}:{article_number}"
        return law_name

    def should_clear_cache(self) -> bool:
        """Check if cache should be cleared due to memory pressure"""
        return self.memory_monitor.is_memory_limit_exceeded()

    def cleanup_if_needed(self) -> None:
        """Cleanup expired entries and manage memory"""
        if self.should_clear_cache():
            logger.info("Memory limit exceeded, clearing caches")
            self.law_lookup_cache.clear()
            self.law_content_cache.clear()
            self.article_cache.clear()
        else:
            self.law_lookup_cache.cleanup_expired()
            self.law_content_cache.cleanup_expired()
            self.article_cache.cleanup_expired()

    async def prefetch_common_articles(self, client: httpx.AsyncClient) -> None:
        """Prefetch commonly accessed articles"""
        logger.info("Starting prefetch of common articles")

        for law_name, article_number in self.common_articles:
            cache_key = self.get_cache_key(law_name, article_number)

            # Skip if already cached
            if self.article_cache.get(cache_key):
                continue

            try:
                # Get law number
                law_num = await self._get_law_number(law_name, client)
                if not law_num:
                    continue

                # Get law content
                law_content = await self._get_law_content(law_num, client)
                if law_content:
                    # Store in cache
                    self.law_content_cache.put(law_num, law_content)
                    logger.debug(f"Prefetched {law_name} content")

            except Exception as e:
                logger.warning(f"Failed to prefetch {law_name}: {e}")

    async def _get_law_number(self, law_name: str, client: httpx.AsyncClient) -> Optional[str]:
        """Get law number with caching"""
        cache_key = self.get_cache_key(law_name)

        # Check cache first
        cached_num = self.law_lookup_cache.get(cache_key)
        if cached_num:
            return cached_num

        # Check direct mapping
        if law_name in BASIC_LAWS:
            law_num = BASIC_LAWS[law_name]
            self.law_lookup_cache.put(cache_key, law_num)
            return law_num

        # API lookup
        try:
            best_law = await search_law_by_title(client, law_name)
            if best_law:
                law_num = best_law.get("law_info", {}).get("law_num")
                if law_num:
                    self.law_lookup_cache.put(cache_key, law_num)
                    return law_num

        except Exception as e:
            logger.error(f"Failed to get law number for {law_name}: {e}")

        return None

    async def _get_law_content(self, law_num: str, client: httpx.AsyncClient) -> Optional[dict]:
        """Get law content with caching"""
        # Check cache first
        cached_content = self.law_content_cache.get(law_num)
        if cached_content:
            return cached_content

        try:
            response = await client.get(f"/law_data/{law_num}", params={
                "law_full_text_format": "xml"
            })
            response.raise_for_status()

            data = json.loads(response.text)
            self.law_content_cache.put(law_num, data)
            return data

        except Exception as e:
            logger.error(f"Failed to get law content for {law_num}: {e}")

        return None

    async def batch_request_laws(self, law_names: list[str], client: httpx.AsyncClient) -> dict[str, str]:
        """Batch request multiple law numbers"""
        results = {}

        # Separate cached and non-cached requests
        cached_requests = []
        api_requests = []

        for law_name in law_names:
            cache_key = self.get_cache_key(law_name)
            cached_num = self.law_lookup_cache.get(cache_key)

            if cached_num:
                results[law_name] = cached_num
                cached_requests.append(law_name)
            else:
                api_requests.append(law_name)

        logger.info(f"Batch request: {len(cached_requests)} cached, {len(api_requests)} API requests")

        # Process uncached requests
        if api_requests:
            # Group similar requests to reduce API calls
            unique_requests = list(set(api_requests))

            for law_name in unique_requests:
                try:
                    law_num = await self._get_law_number(law_name, client)
                    if law_num:
                        results[law_name] = law_num
                        # Also cache for other identical requests
                        for other_law in api_requests:
                            if other_law == law_name:
                                results[other_law] = law_num
                except Exception as e:
                    logger.error(f"Failed to get law number for {law_name} in batch: {e}")

        return results

# API configuration
API_URL = os.environ.get("EGOV_API_URL", "https://laws.e-gov.go.jp/api/2")
API_TOKEN = os.environ.get("EGOV_API_TOKEN", "")

# Create MCP server with Windows-compatible configuration
mcp = FastMCP(
    name=os.environ.get("MCP_SERVER_NAME", "e-Gov Law API Server v2"),
    mask_error_details=True,  # Security: mask internal error details
    on_duplicate_tools="warn",
    on_duplicate_resources="warn",
    on_duplicate_prompts="warn"
)

class ConfigLoader:
    """
    Configuration loader for law mappings with backward compatibility.

    Loads law aliases and basic laws from YAML configuration file.
    Falls back to hardcoded values for backward compatibility.
    """

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize ConfigLoader with optional custom config path.

        Args:
            config_path: Path to YAML config file. If None, uses environment variable
                        LAW_CONFIG_PATH or defaults to config/laws.yaml
        """
        # Windows-compatible path handling
        default_config = Path("config") / "laws.yaml"
        config_env = os.environ.get("LAW_CONFIG_PATH")
        if config_env:
            self.config_path = Path(config_env)
        elif config_path:
            self.config_path = Path(config_path)
        else:
            self.config_path = default_config
        self._law_aliases: Optional[dict[str, str]] = None
        self._basic_laws: Optional[dict[str, str]] = None

        # Fallback hardcoded values for backward compatibility
        self._fallback_law_aliases = {
            # 一般的な略称
            "道交法": "道路交通法",
            "労基法": "労働基準法",
            "独禁法": "独占禁止法",
            "消契法": "消費者契約法",
            "著作権": "著作権法",
            "特許": "特許法",
            "建基法": "建築基準法",
            "憲法": "日本国憲法",

            # 分野別検索
            "税法": "所得税法",
            "労働法": "労働基準法",
            "知財法": "著作権法",
            "交通法": "道路交通法",

            # 一般的な呼び方
            "会社": "会社法",
            "民事": "民法",
            "刑事": "刑法",
            "訴訟": "民事訴訟法",
        }

        self._fallback_basic_laws = {
            # 六法 (Six Codes)
            "民法": "明治二十九年法律第八十九号",
            "憲法": "昭和二十一年憲法",
            "日本国憲法": "昭和二十一年憲法",
            "刑法": "明治四十年法律第四十五号",
            "商法": "明治三十二年法律第四十八号",
            "民事訴訟法": "平成八年法律第百九号",
            "刑事訴訟法": "昭和二十三年法律第百三十一号",

            # 現代重要法 (Modern Key Laws)
            "会社法": "平成十七年法律第八十六号",
            "労働基準法": "昭和二十二年法律第四十九号",
            "所得税法": "昭和四十年法律第三十三号",
            "法人税法": "昭和四十年法律第三十四号",
            "著作権法": "昭和四十五年法律第四十八号",
            "特許法": "昭和三十四年法律第百二十一号",
            "道路交通法": "昭和三十五年法律第百五号",
            "建築基準法": "昭和二十五年法律第二百一号",
            "独占禁止法": "昭和二十二年法律第五十四号",
            "消費者契約法": "平成十二年法律第六十一号",
            "特定受託事業者に係る取引の適正化等に関する法律": "令和五年法律第二十五号",
            # 税法 (Tax Laws)
            "消費税法": "昭和六十三年法律第百八号",
            "相続税法": "昭和二十五年法律第七十三号",
            "地方税法": "昭和二十五年法律第二百二十六号",
            "国税通則法": "昭和三十七年法律第六十六号",
            "租税特別措置法": "昭和三十二年法律第二十六号",
        }

    def _load_config(self) -> dict[str, Any]:
        """Load configuration from YAML file with Windows support."""
        try:
            if self.config_path.exists():
                # Windows-compatible UTF-8 file reading
                with open(self.config_path, encoding='utf-8', newline='') as f:
                    config = yaml.safe_load(f)
                    logger.info(f"Loaded configuration from {self.config_path}")
                    return config or {}
            else:
                logger.warning(f"Config file not found at {self.config_path}, using fallback values")
                return {}
        except Exception as e:
            logger.error(f"Failed to load config from {self.config_path}: {e}")
            logger.info("Using fallback values for backward compatibility")
            return {}

    @property
    def law_aliases(self) -> dict[str, str]:
        """Get law aliases mapping."""
        if self._law_aliases is None:
            config = self._load_config()
            self._law_aliases = config.get('law_aliases', self._fallback_law_aliases)
        return self._law_aliases

    @property
    def basic_laws(self) -> dict[str, str]:
        """Get basic laws mapping."""
        if self._basic_laws is None:
            config = self._load_config()
            self._basic_laws = config.get('basic_laws', self._fallback_basic_laws)
        return self._basic_laws

    def reload_config(self) -> None:
        """Reload configuration from file."""
        self._law_aliases = None
        self._basic_laws = None
        logger.info("Configuration reloaded")

# Initialize global config loader, prompt loader, and cache manager
config_loader = ConfigLoader()
prompt_loader = PromptLoader()
cache_manager = CacheManager()

# LAW ALIASES MAPPING (略称・通称から正式名称へ) - now loaded from config
LAW_ALIASES = config_loader.law_aliases

# COMPREHENSIVE BASIC LAWS MAPPING (16 major laws) - now loaded from config
BASIC_LAWS = config_loader.basic_laws

async def get_http_client() -> httpx.AsyncClient:
    """Create HTTP client for e-Gov API."""
    headers = {
        "User-Agent": "e-Gov-Law-MCP-v2/2.0",
        "Accept": "application/json"
    }
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"

    return httpx.AsyncClient(
        base_url=API_URL,
        headers=headers,
        timeout=30.0,
        follow_redirects=True
    )

def extract_text_from_xml(obj) -> str:
    """
    Smart text extraction from e-Gov API response.
    Handles both Base64-encoded XML and structured JSON.
    """
    if isinstance(obj, str):
        # Handle Base64-encoded XML (XML format response)
        try:
            xml_bytes = base64.b64decode(obj)
            xml_string = xml_bytes.decode('utf-8')
            root = ET.fromstring(xml_string)

            def extract_xml_text(element):
                text = element.text or ''
                for child in element:
                    text += extract_xml_text(child)
                text += element.tail or ''
                return text

            return extract_xml_text(root)
        except Exception as e:
            logger.warning(f"XML decode failed: {e}")
            return str(obj)

    elif isinstance(obj, dict):
        # Handle structured JSON response
        if 'children' in obj:
            return ''.join(extract_text_from_xml(child) for child in obj['children'])
        elif 'text' in obj:
            return obj['text']
        return str(obj)

    elif isinstance(obj, list):
        return ''.join(extract_text_from_xml(item) for item in obj)

    return str(obj)

def arabic_to_kanji(num_str: str) -> str:
    """Convert Arabic numbers to Kanji for Japanese legal text."""
    if not num_str.isdigit():
        return num_str

    num = int(num_str)
    if num == 0: return '〇'
    if 1 <= num <= 9: return '一二三四五六七八九'[num-1]
    if 10 <= num <= 19:
        return '十' if num == 10 else '十' + '一二三四五六七八九'[num%10-1]
    if 20 <= num <= 99:
        tens = '二三四五六七八九'[num//10-2] + '十'
        ones = '' if num % 10 == 0 else '一二三四五六七八九'[num%10-1]
        return tens + ones
    if 100 <= num <= 999:
        hundreds = '百' if num // 100 == 1 else '一二三四五六七八九'[num//100-1] + '百'
        remainder = num % 100
        if remainder == 0: return hundreds
        if remainder < 10: return hundreds + '一二三四五六七八九'[remainder-1]
        return hundreds + arabic_to_kanji(str(remainder))

    return num_str  # Fallback for large numbers

def generate_search_patterns(article_input: str) -> list[str]:
    """Generate comprehensive search patterns for article numbers."""
    # Extract main number and patterns
    main_match = re.search(r'(\d+)', article_input)
    if not main_match:
        return [article_input]

    main_num = main_match.group(1)
    kanji_num = arabic_to_kanji(main_num)

    patterns = [
        f"第{kanji_num}条",     # e.g., 第百九十二条
        f"{kanji_num}条",      # e.g., 百九十二条
        f"第{main_num}条",     # e.g., 第192条
    ]

    # Handle 条の2 patterns
    if 'の' in article_input:
        no_match = re.search(r'の(\d+)', article_input)
        if no_match:
            no_num = no_match.group(1)
            no_kanji = arabic_to_kanji(no_num)
            patterns.extend([
                f"第{kanji_num}条の{no_kanji}",  # e.g., 第三百二十五条の三
                f"{kanji_num}条の{no_kanji}",   # e.g., 三百二十五条の三
                f"第{main_num}条の{no_num}",    # e.g., 第325条の3
            ])

    # Handle 項・号 patterns
    if '項' in article_input:
        kou_match = re.search(r'第(\d+)項', article_input)
        if kou_match:
            kou_num = kou_match.group(1)
            kou_kanji = arabic_to_kanji(kou_num)
            patterns.extend([
                f"第{kanji_num}条第{kou_kanji}項",
                f"第{main_num}条第{kou_num}項",
            ])

    if '号' in article_input:
        gou_match = re.search(r'第(\d+)号', article_input)
        if gou_match:
            gou_num = gou_match.group(1)
            gou_kanji = arabic_to_kanji(gou_num)
            patterns.extend([
                f"第{kanji_num}条第{gou_kanji}号",
                f"第{main_num}条第{gou_num}号",
            ])

    # Add original patterns
    patterns.extend([
        f"第{article_input}条",
        f"{article_input}条",
        article_input
    ])

    # Remove duplicates while preserving order
    return list(dict.fromkeys(patterns))


# ---------------------------------------------------------------------------
# 法令の選択
#
# e-Gov API v2 では、法令の題名は revision_info.law_title にあり、law_info には
# 法令番号・法令ID・公布日しかない。また /laws の law_title は部分一致で検索される
# ため、「消費税法」で検索すると、題名に「消費税法」を含む改正法や特例法
# （平成二十四年法律第六十八号など）も候補に入る。
# そのため、題名が入力と一致する候補を、他のどの条件よりも優先する。
# ---------------------------------------------------------------------------


def get_law_title(law: dict[str, Any]) -> str:
    """e-Gov API v2 のレコード（/laws の 1 件、または /law_data の応答）から題名を返す。"""
    revision_info = law.get("revision_info") or {}
    return revision_info.get("law_title") or ""


def get_law_abbrevs(law: dict[str, Any]) -> set[str]:
    """登録された略称（revision_info.abbrev。カンマ区切り）を返す。"""
    abbrev = (law.get("revision_info") or {}).get("abbrev") or ""
    return {a.strip() for a in re.split(r"[,，、]", abbrev) if a.strip()}


def is_law_repealed(law: dict[str, Any]) -> bool:
    """廃止・失効の印があれば True（廃止されていない場合、API は文字列 "None" を返す）。"""
    status = (law.get("revision_info") or {}).get("repeal_status")
    return status not in (None, "", "None")


def _title_equals(title: str, name: str) -> bool:
    """題名の完全一致。「昭和二十二年法律第五十四号（…）」の形の題名にも対応する。"""
    return title == name or title.endswith(f"（{name}）")


def law_title_matches(law: dict[str, Any], law_name: str) -> bool:
    """
    レコードが law_name という名前の法令であれば True。

    題名の完全一致、「法令番号（題名）」の形（独占禁止法など）、登録された略称の
    いずれかで判定する。
    """
    name = law_name.strip()
    if not name:
        return False
    return _title_equals(get_law_title(law), name) or name in get_law_abbrevs(law)


def select_best_law(laws: list[dict[str, Any]], law_name: str) -> Optional[dict[str, Any]]:
    """
    /laws の検索結果から、law_name に最もよく当たる法令を選ぶ。

    優先順位: 題名の完全一致 > 略称の一致 > 廃止されていない > 題名が短い
    （その法令を題名に含むだけの法令より、本体の法令を選ぶ） > 公布日が新しい
    """
    if not laws:
        return None
    name = law_name.strip()

    def sort_key(law: dict[str, Any]) -> tuple[bool, bool, bool, int, str]:
        title = get_law_title(law)
        return (
            _title_equals(title, name),
            name in get_law_abbrevs(law),
            not is_law_repealed(law),
            -len(title) if title else -(10**6),
            (law.get("law_info") or {}).get("promulgation_date") or "",
        )

    return max(laws, key=sort_key)


async def search_law_by_title(client: httpx.AsyncClient, law_name: str) -> Optional[dict[str, Any]]:
    """
    題名で /laws を検索し、最もよく当たる法令のレコードを返す。

    まず法律（law_type=Act）に絞って検索する。題名が一致する法律がなければ、種別を絞らずに
    検索し直す（日本国憲法や、施行令・施行規則などは Act ではないため）。
    """
    response = await client.get(
        "/laws", params={"law_title": law_name, "law_type": "Act", "limit": 20}
    )
    response.raise_for_status()
    laws = json.loads(response.text).get("laws", [])
    best_law = select_best_law(laws, law_name)

    if best_law is None or not law_title_matches(best_law, law_name):
        response = await client.get("/laws", params={"law_title": law_name, "limit": 20})
        response.raise_for_status()
        any_type_laws = json.loads(response.text).get("laws", [])
        any_type_best = select_best_law(any_type_laws, law_name)
        if any_type_best is not None and (
            best_law is None or law_title_matches(any_type_best, law_name)
        ):
            best_law = any_type_best
            laws = any_type_laws

    if best_law is None:
        return None

    logger.info(f"Found {len(laws)} candidate laws for '{law_name}'")
    for i, law in enumerate(laws[:3]):  # 上位 3 件をログに残す
        logger.info(
            f"  Candidate {i+1}: {get_law_title(law) or 'N/A'} ({(law.get('law_info') or {}).get('law_num', 'N/A')})"
        )
    if not law_title_matches(best_law, law_name):
        logger.warning(
            f"No exact title match for '{law_name}'; using closest candidate: "
            f"{get_law_title(best_law)} ({(best_law.get('law_info') or {}).get('law_num')})"
        )
    return best_law


# ---------------------------------------------------------------------------
# 条文の構造的な取り出し
#
# 全文を平文にして「第一条」を探すと、目次（TOC）や、条番号を第一条から振り直す
# 附則（SupplProvision）にも当たる。法令 XML ではこれらが要素で区別されているので、
# 本則（MainProvision）の Article/@Num で条を特定する。
# ---------------------------------------------------------------------------

_KANJI_DIGITS = {
    "〇": 0,
    "零": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_KANJI_UNITS = {"十": 10, "百": 100, "千": 1000}
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def kanji_to_arabic(kanji: str) -> int:
    """漢数字（例: 「三百二十五」）を整数（325）に変換する。"""
    total, current = 0, 0
    for ch in kanji:
        if ch in _KANJI_DIGITS:
            current = current * 10 + _KANJI_DIGITS[ch]
        elif ch in _KANJI_UNITS:
            total += (current or 1) * _KANJI_UNITS[ch]
            current = 0
    return total + current


def normalize_article_number(article_input: str) -> str:
    """条番号の指定に含まれる全角数字・漢数字を半角数字にそろえる。"""
    text = article_input.strip().translate(_FULLWIDTH_DIGITS)
    return re.sub(
        r"[〇零一二三四五六七八九十百千]+", lambda m: str(kanji_to_arabic(m.group(0))), text
    )


def _branch_num(main: str, branches: str) -> str:
    """「325」と「の3」から XML の Num 表記「325_3」を作る。"""
    return "_".join([main] + re.findall(r"[0-9]+", branches))


class ArticleReference(TypedDict):
    """parse_article_reference の戻り値。article と item は XML の Num 表記（例: "325_3"）"""

    article: str
    paragraph: Optional[str]
    item: Optional[str]


def parse_article_reference(article_input: str) -> Optional[ArticleReference]:
    """
    条番号の指定を解釈する。

    受け付ける形は「（第）N（条）（のM）…（第P項）（第Q号（のR）…）」で、全角数字・漢数字・空白も使える。
    例: "192"、"325条の3"、"第9条第2項"、"第二条第一項第三号"、"2条3号の2"。
    「条」を省けるのは条番号だけのとき（"192"、"325の3"）に限る（「12項」を「1条2項」と読まない）。

    次の指定は受け付けず、None を返す: 号の細目（"第2条第1項第3号イ"）、附則・前文、
    法令名を含む指定（"民法第192条"）、範囲や列挙（"192-193"）、50 文字を超える入力。
    """
    # Python の整数と文字列の変換の桁数上限（4300 桁）に触れないため
    if len(article_input) > 50:
        return None
    text = re.sub(r"\s", "", normalize_article_number(article_input))
    m = re.fullmatch(
        r"第?([0-9]+)(条)?((?:の[0-9]+)*)(?:第?([0-9]+)項)?(?:第?([0-9]+)号((?:の[0-9]+)*))?", text
    )
    if m is None:
        return None
    article, has_article_mark, article_branches, paragraph, item, item_branches = m.groups()
    if has_article_mark is None and (paragraph or item):
        return None
    return {
        "article": _branch_num(article, article_branches),
        "paragraph": paragraph,
        "item": _branch_num(item, item_branches) if item else None,
    }


def format_article_label(num: str) -> str:
    """XML の Num 表記を表示用にする（"325_3" -> "第325条の3"）。"""
    main, *branches = num.split("_")
    return f"第{main}条" + "".join(f"の{b}" for b in branches)


def _num_key(num: str) -> Optional[tuple[int, ...]]:
    parts = num.split("_")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else None


def _article_num_matches(xml_num: str, wanted: str) -> bool:
    """Num が一致するか、削除された条の範囲表記（例: Num="38:84"）に含まれれば True。"""
    if xml_num == wanted:
        return True
    if ":" not in xml_num:
        return False
    start, end = (_num_key(n) for n in xml_num.split(":", 1))
    key = _num_key(wanted)
    if start is None or end is None or key is None:
        return False
    return start <= key <= end


def parse_law_xml(law_full_text: Any) -> Optional[ET.Element]:
    """/law_data（law_full_text_format=xml）が返す Base64 の法令 XML を解析する。"""
    if not isinstance(law_full_text, str):
        return None
    try:
        return ET.fromstring(base64.b64decode(law_full_text).decode("utf-8"))
    except Exception as e:
        logger.warning(f"Failed to parse law XML: {e}")
        return None


_TITLE_TAGS = {"ParagraphNum", "ItemTitle"} | {f"Subitem{i}Title" for i in range(1, 11)}
_SENTENCE_TAGS = {"ParagraphSentence", "ItemSentence"} | {
    f"Subitem{i}Sentence" for i in range(1, 11)
}
_BLOCK_TAGS = {"Paragraph", "Item"} | {f"Subitem{i}" for i in range(1, 11)}


def _xml_inline_text(element: ET.Element) -> str:
    """要素の文字列。ルビの読み（<Rt>）と、整形用の改行・字下げを除く。"""

    def collect(e: ET.Element) -> str:
        parts = [e.text or ""]
        for child in e:
            if child.tag != "Rt":
                parts.append(collect(child))
            parts.append(child.tail or "")
        return "".join(parts)

    return re.sub(r"\s*\n\s*", "", collect(element)).strip()


def _xml_sentence_text(element: ET.Element) -> str:
    """文の要素の文字列。Column（号の「用語　定義」など）は全角空白で区切る。"""
    columns = element.findall("Column")
    if columns:
        return "　".join(_xml_inline_text(c) for c in columns)
    return _xml_inline_text(element)


def _render_provision(element: ET.Element, lines: list[str], lead: str = "") -> None:
    """条・項・号などの要素を、法令集と同じ体裁（「第一条　…」「２　…」「一　…」）で lines に書き出す。"""
    if element.tag == "Article":
        title = ""
        for child in element:
            if child.tag == "ArticleCaption":
                lines.append(_xml_inline_text(child))
            elif child.tag == "ArticleTitle":
                title = _xml_inline_text(child)
        title = lead or title
        first = True
        for child in element:
            if child.tag in ("ArticleCaption", "ArticleTitle"):
                continue
            if first and child.tag == "Paragraph":
                _render_provision(child, lines, lead=title)
                first = False
            else:
                _render_provision(child, lines)
        if first and title:
            lines.append(title)
        return

    if element.tag in _BLOCK_TAGS:
        title, body, rest = "", "", []
        for child in element:
            if child.tag in _TITLE_TAGS:
                title = _xml_inline_text(child)
            elif child.tag in _SENTENCE_TAGS:
                body = _xml_sentence_text(child)
            elif child.tag.endswith("Caption"):
                lines.append(_xml_inline_text(child))
            else:
                rest.append(child)
        if lead and title:
            lines.append(lead)
            head = title
        else:
            head = lead or title
        lines.append(f"{head}　{body}" if head and body else (head or body))
        for child in rest:
            _render_provision(child, lines)
        return

    # 表・リスト・改正規定の文などは、要素ごとに 1 行で文字列を残す
    text = "　".join(t.strip() for t in element.itertext() if t.strip())
    if text:
        lines.append(text)


def find_article_in_law_xml(root: ET.Element, article_input: str) -> dict[str, Any]:
    """
    法令 XML の本則から条（必要に応じて項・号）を探す。

    {"status": ..., "text": str | None, "article_num": str | None} を返す。
    status は "found"、"article_not_found"、"paragraph_not_found"、"item_not_found"、
    "invalid_reference"（解釈できない指定。parse_article_reference を参照）のいずれか。
    """
    ref = parse_article_reference(article_input)
    if ref is None:
        return {"status": "invalid_reference", "text": None, "article_num": None}
    result: dict[str, Any] = {
        "status": "article_not_found",
        "text": None,
        "article_num": ref["article"],
    }

    articles: list[ET.Element] = []
    main = root.find(".//MainProvision")
    if main is not None:
        # 改正規定（AmendProvision）の中に引用された条は、この法令自体の条ではない
        quoted = {id(a) for ap in main.iter("AmendProvision") for a in ap.iter("Article")}
        articles = [a for a in main.iter("Article") if id(a) not in quoted]

    article = next(
        (a for a in articles if _article_num_matches(a.get("Num") or "", ref["article"])), None
    )
    if article is None:
        return result

    caption = article.find("ArticleCaption")
    title_element = article.find("ArticleTitle")
    article_title = _xml_inline_text(title_element) if title_element is not None else ""
    lines: list[str] = []

    if ref["paragraph"] is None and ref["item"] is None:
        _render_provision(article, lines)
    else:
        paragraph_num = ref["paragraph"] or "1"
        paragraph = next(
            (p for p in article.findall("Paragraph") if p.get("Num") == paragraph_num), None
        )
        if paragraph is None:
            result["status"] = "paragraph_not_found"
            return result
        if caption is not None:
            lines.append(_xml_inline_text(caption))
        # 見出しは「第九条第二項」の形にする（憲法のように項番号が印字されない法令もあるため）
        lead = article_title
        if ref["paragraph"] is not None and paragraph_num != "1":
            lead += f"第{arabic_to_kanji(paragraph_num)}項"
        if ref["item"] is None:
            _render_provision(paragraph, lines, lead=lead)
        else:
            item = next((i for i in paragraph.findall("Item") if i.get("Num") == ref["item"]), None)
            if item is None:
                result["status"] = "item_not_found"
                return result
            if ref["paragraph"] is not None and paragraph_num == "1":
                lead += "第一項"
            lines.append(lead)
            _render_provision(item, lines)

    result.update(status="found", text="\n".join(line for line in lines if line))
    return result


def extract_article(law_full_text: Any, article_number: str) -> dict[str, Any]:
    """
    /law_data の law_full_text から、本則の条文を取り出す。

    戻り値は find_article_in_law_xml と同じ。XML として解析できない応答は、
    推測した条文を返さず ToolError にする。
    """
    root = parse_law_xml(law_full_text)
    if root is None:
        raise ToolError("Failed to parse the law text (XML) returned by the e-Gov API")
    return find_article_in_law_xml(root, article_number)


async def smart_law_lookup(law_name: str) -> Optional[str]:
    """Smart law lookup with formal name verification and direct mapping fallback to search."""
    law_name_clean = law_name.strip()
    original_input = law_name_clean

    # Step 1: Check for aliases and convert to formal name
    if law_name_clean in LAW_ALIASES:
        formal_name = LAW_ALIASES[law_name_clean]
        logger.info(f"Alias conversion: '{original_input}' -> '{formal_name}'")
        law_name_clean = formal_name

    # Step 2: Check cache first
    cache_key = cache_manager.get_cache_key(law_name_clean)
    cached_result = cache_manager.law_lookup_cache.get(cache_key)
    if cached_result:
        logger.info(f"Cache hit for law lookup: {law_name_clean} -> {cached_result}")
        return cached_result

    # Step 3: Check direct mapping with formal name
    if law_name_clean in BASIC_LAWS:
        result = BASIC_LAWS[law_name_clean]
        logger.info(f"Direct mapping: {law_name_clean} -> {result}")
        # Cache the result
        cache_manager.law_lookup_cache.put(cache_key, result)
        return result

    # Step 4: Intelligent search for unknown laws
    async with await get_http_client() as client:
        best_law = await search_law_by_title(client, law_name_clean)
        if best_law is None:
            logger.warning(f"No laws found for search term: {law_name_clean} (original: {original_input})")
            return None

        selected_law_num = (best_law.get("law_info") or {}).get("law_num")
        logger.info(f"Selected law: {get_law_title(best_law)} ({selected_law_num}) for search term '{law_name_clean}' (original: '{original_input}')")

        # Cache the result
        if selected_law_num:
            cache_manager.law_lookup_cache.put(cache_key, selected_law_num)

        return selected_law_num

@mcp.tool
async def find_law_article(law_name: str, article_number: str, ctx: Context) -> dict:
    """
    Find a specific article in Japanese law (ULTRA SMART & FAST)

    Supports 16+ major laws with direct mapping for instant access.
    Handles complex patterns like 条の2, 項, 号 automatically.

    Args:
        law_name: Law name (e.g., "民法", "会社法", "憲法")
        article_number: Article number (e.g., "192", "325条の3", "第9条第2項")
        ctx: FastMCP context for logging and progress reporting

    Returns:
        Dict with found article content and legal analysis metadata
    """
    if not law_name or not law_name.strip():
        raise ToolError("law_name is required")
    if not article_number or not article_number.strip():
        raise ToolError("article_number is required")

    try:
        # Log the search request
        await ctx.info(f"Searching for article {article_number} in {law_name}")
        
        # Cleanup cache if needed
        cache_manager.cleanup_if_needed()

        # Step 1: Smart law lookup with formal name verification
        original_law_input = law_name
        formal_law_name = law_name
        name_conversion_applied = False

        # Check if alias conversion is needed
        if law_name.strip() in LAW_ALIASES:
            formal_law_name = LAW_ALIASES[law_name.strip()]
            name_conversion_applied = True
            await ctx.debug(f"Alias conversion: {law_name} → {formal_law_name}")

        law_num = await smart_law_lookup(law_name)
        if not law_num:
            await ctx.error(f"Law '{law_name}' not found")
            raise ToolError(f"Law '{law_name}' not found")

        # Step 2: Get law text with XML format
        async with await get_http_client() as client:
            # Get law data with XML format (elm parameter removed due to API 400 errors)
            response = await client.get(f"/law_data/{law_num}", params={
                "law_full_text_format": "xml"
            })

            response.raise_for_status()

            data = json.loads(response.text)
            law_full_text = data.get('law_full_text', {})

            # Step 3: 本則（MainProvision）から条文を取り出す
            article = extract_article(law_full_text, article_number)
            matches = [article["text"]] if article["status"] == "found" else []

            # Format result with formal name verification info
            law_info_data = data.get('law_info', {})
            actual_law_title = get_law_title(data)
            law_title_verified = law_title_matches(data, formal_law_name)

            result = {
                "law_info": law_info_data,
                "search_law_name": original_law_input,
                "formal_law_name_used": formal_law_name,
                "actual_law_title": actual_law_title,
                "law_title_verified": law_title_verified,
                "name_conversion_applied": name_conversion_applied,
                "search_article": article_number,
                "found_law": actual_law_title,
                "law_number": law_num,
                "matches_found": len(matches),
                "articles": matches[:3] if matches else [],
                "note": f"Searched for article '{article_number}' in '{actual_law_title}'{' (converted from: ' + original_law_input + ')' if name_conversion_applied else ''}",
                "legal_analysis_instruction": prompt_loader.get_legal_analysis_instruction()
            }
            if not law_title_verified:
                result["law_title_warning"] = (
                    f"Requested '{formal_law_name}' but the retrieved law is '{actual_law_title}' ({law_num}). "
                    "Verify that this is the intended law."
                )
                await ctx.warning(result["law_title_warning"])

            if not matches:
                status = article["status"]
                if status == "invalid_reference":
                    result["suggestion"] = (
                        "Could not interpret the article number. "
                        "Use forms such as '192', '325条の3', '第9条第2項' or '第2条第1項第3号の2'. "
                        "Only articles in the main provision (本則) can be specified; "
                        "supplementary provisions (附則) and sub-items such as イ or ロ are not supported "
                        "(specify up to the item, 号)."
                    )
                else:
                    article_label = format_article_label(article["article_num"])
                    if status == "paragraph_not_found":
                        result["suggestion"] = (
                            f"{article_label} exists in {actual_law_title}, but the specified paragraph (項) does not exist."
                        )
                    elif status == "item_not_found":
                        result["suggestion"] = (
                            f"{article_label} exists in {actual_law_title}, but the specified subparagraph (号) does not exist."
                        )
                    else:
                        result["suggestion"] = (
                            f"{article_label} was not found in the main provision of {actual_law_title}. Please verify the article number."
                        )

            await ctx.info(f"Successfully found article {article_number} in {result.get('actual_law_title', law_name)}")
            return result

    except ToolError:
        # Re-raise ToolError to send proper error to client
        raise
    except Exception as e:
        logger.error(f"Find law article error: {e}")
        await ctx.error(f"Search failed: {str(e)}")
        raise ToolError(f"Search failed: {str(e)}")

@mcp.tool
async def search_laws(
    law_title: str = "",
    law_type: str = "",
    law_num: str = "",
    limit: int = 10,
    offset: int = 0,
    ctx: Context = None
) -> dict:
    """
    Search Japanese laws with smart filtering

    Args:
        law_title: Law title (partial match)
        law_type: Law type (Act, CabinetOrder, etc.)
        law_num: Law number (partial match)
        limit: Maximum results (1-500)
        offset: Starting position
        ctx: FastMCP context for logging

    Returns:
        Dict with search results
    """
    # Input validation
    if limit < 1 or limit > 500:
        raise ToolError("limit must be between 1 and 500")
    if offset < 0:
        raise ToolError("offset must be 0 or greater")
    
    if ctx:
        await ctx.info(f"Searching laws with title='{law_title}', type='{law_type}', limit={limit}")

    params = {"limit": limit, "offset": offset}
    if law_title: params["law_title"] = law_title
    if law_type: params["law_type"] = law_type
    if law_num: params["law_num"] = law_num

    try:
        async with await get_http_client() as client:
            response = await client.get("/laws", params=params)
            response.raise_for_status()
            
            # Parse JSON and return dict for FastMCP auto-serialization
            result = json.loads(response.text)
            if ctx:
                law_count = len(result.get("laws", []))
                await ctx.info(f"Found {law_count} laws matching search criteria")
            return result
    except Exception as e:
        logger.error(f"Search laws error: {e}")
        if ctx:
            await ctx.error(f"Search failed: {str(e)}")
        raise ToolError(f"Search failed: {str(e)}")

@mcp.tool
async def search_laws_by_keyword(keyword: str, law_type: str = "", limit: int = 5, ctx: Context = None) -> dict:
    """
    Full-text keyword search in Japanese laws

    Args:
        keyword: Search keyword (required)
        law_type: Law type filter (optional)
        limit: Maximum results (1-20)
        ctx: FastMCP context for logging

    Returns:
        Dict with search results
    """
    if not keyword or not keyword.strip():
        raise ToolError("keyword is required")
    if limit < 1 or limit > 20:
        raise ToolError("limit must be between 1 and 20")
    
    if ctx:
        await ctx.info(f"Searching for keyword: '{keyword}' with limit={limit}")

    params = {"keyword": keyword.strip(), "limit": limit}
    if law_type: params["law_type"] = law_type

    try:
        async with await get_http_client() as client:
            response = await client.get("/keyword", params=params)
            response.raise_for_status()
            
            # Parse JSON and return dict for FastMCP auto-serialization
            result = json.loads(response.text)
            if ctx:
                result_count = len(result.get("laws", []))
                await ctx.info(f"Found {result_count} laws containing keyword '{keyword}'")
            return result
    except Exception as e:
        logger.error(f"Keyword search error: {e}")
        if ctx:
            await ctx.error(f"Keyword search failed: {str(e)}")
        raise ToolError(f"Keyword search failed: {str(e)}")

@mcp.tool
async def get_law_content(law_id: str = "", law_num: str = "", response_format: str = "json", elm: str = "", ctx: Context = None) -> dict:
    """
    Get law content (optimized per API spec with size limits)

    Args:
        law_id: Law ID
        law_num: Law number
        response_format: "json" or "xml"
        elm: Element to retrieve (currently disabled due to API 400 errors)
        ctx: FastMCP context for logging

    Returns:
        Dict with law content. For large laws (>800KB), returns summary with recommendation to use find_law_article for specific articles.

    Note:
        - elm parameter is currently disabled due to e-Gov API 400 errors
        - Large laws like Company Law (会社法) will return a summary instead of full text
        - Use find_law_article tool for specific article searches in large laws
    """
    if not law_id and not law_num:
        raise ToolError("Either law_id or law_num must be specified")
    if response_format not in ["json", "xml"]:
        raise ToolError("response_format must be 'json' or 'xml'")

    law_identifier = law_id if law_id else law_num
    
    if ctx:
        await ctx.info(f"Getting law content for {law_identifier} in {response_format} format")
    params = {}
    if response_format == "xml":
        params["law_full_text_format"] = "xml"

    # Note: elm parameter causes 400 errors with current e-Gov API
    # Commented out to avoid API errors
    # if elm:
    #     params["elm"] = elm

    try:
        async with await get_http_client() as client:
            response = await client.get(f"/law_data/{law_identifier}", params=params)
            response.raise_for_status()

            if response_format == "json":
                # Format JSON response for better readability
                data = json.loads(response.text)

                # Check response size and truncate if necessary
                response_str = json.dumps(data, ensure_ascii=False, indent=2)
                if len(response_str) > 800000:  # 800KB limit (留余裕給其他數據)
                    # Create summary instead of full text for large laws
                    law_info = data.get('law_info', {})
                    summary = {
                        "law_info": law_info,
                        "warning": "法令全文が長すぎるため、概要のみ表示しています。",
                        "recommendation": "特定の条文を検索する場合は find_law_article ツールを使用してください。",
                        "law_stats": {
                            "original_size_bytes": len(response_str),
                            "law_title": get_law_title(data),
                            "law_num": law_info.get('law_num', ''),
                            "promulgation_date": law_info.get('promulgation_date', '')
                        }
                    }

                    # Try to include table of contents if available
                    law_full_text = data.get('law_full_text', {})
                    if isinstance(law_full_text, dict):
                        # Extract structure information
                        if 'chapters' in str(law_full_text).lower() or '章' in str(law_full_text):
                            summary["structure_note"] = "この法令は章立て構造を持っています。"
                        if 'sections' in str(law_full_text).lower() or '節' in str(law_full_text):
                            summary["structure_note"] = summary.get("structure_note", "") + " 節による区分があります。"

                    if ctx:
                        await ctx.info(f"Large law content truncated to summary ({len(response_str)} bytes)")
                    return summary

                # For smaller responses, add readable text
                law_full_text = data.get('law_full_text', {})
                if isinstance(law_full_text, str):
                    # Extract readable text from XML
                    data['law_full_text_readable'] = extract_text_from_xml(law_full_text)

                if ctx:
                    await ctx.info(f"Successfully retrieved law content ({len(response_str)} bytes)")
                return data
            else:
                # For XML format, check size and truncate if needed
                if len(response.text) > 800000:
                    if ctx:
                        await ctx.info(f"Large XML content truncated ({len(response.text)} bytes)")
                    return {
                        "format": "xml",
                        "warning": "法令全文が長すぎるため、概要のみ表示しています。",
                        "recommendation": "特定の条文を検索する場合は find_law_article ツールを使用してください。",
                        "original_size_bytes": len(response.text),
                        "truncated_content": response.text[:1000] + "..."
                    }
                else:
                    if ctx:
                        await ctx.info(f"Successfully retrieved XML content ({len(response.text)} bytes)")
                    return {
                        "format": "xml",
                        "content": response.text
                    }

    except Exception as e:
        logger.error(f"Get law content error: {e}")
        if ctx:
            await ctx.error(f"Failed to get law content: {str(e)}")
        raise ToolError(f"Failed to get law content: {str(e)}")

@mcp.tool
async def batch_find_articles(law_article_pairs: str, ctx: Context) -> dict:
    """
    Batch find multiple law articles efficiently

    Args:
        law_article_pairs: JSON string with law-article pairs, e.g. '[{"law":"民法","article":"192"},{"law":"憲法","article":"9"}]'
        ctx: FastMCP context for logging

    Returns:
        Dict with batch results and performance stats
    """
    try:
        pairs = json.loads(law_article_pairs)
        if not isinstance(pairs, list):
            raise ToolError("law_article_pairs must be a JSON array")
            
        await ctx.info(f"Starting batch search for {len(pairs)} law-article pairs")

        results = []
        cache_hits = 0
        api_calls = 0

        async with await get_http_client() as client:
            # Prefetch if cache is empty
            if cache_manager.law_lookup_cache.size() == 0:
                await cache_manager.prefetch_common_articles(client)

            for i, pair in enumerate(pairs):
                if not isinstance(pair, dict) or "law" not in pair or "article" not in pair:
                    results.append({"error": "Invalid pair format"})
                    continue
                    
                await ctx.debug(f"Processing pair {i+1}/{len(pairs)}: {pair['law']} - {pair['article']}")

                law_name = pair["law"]
                article_number = pair["article"]

                # Check cache first
                cache_key = cache_manager.get_cache_key(law_name, article_number)
                cached_result = cache_manager.article_cache.get(cache_key)

                if cached_result:
                    results.append(cached_result)
                    cache_hits += 1
                else:
                    # Perform law article search directly
                    try:
                        # Internal article search logic (similar to find_law_article)
                        if not law_name or not law_name.strip():
                            results.append({"error": "law_name is required"})
                            continue
                        if not article_number or not article_number.strip():
                            results.append({"error": "article_number is required"})
                            continue
                            
                        # Use smart_law_lookup to get law number
                        law_num = await smart_law_lookup(law_name)
                        if not law_num:
                            results.append({"error": f"Law '{law_name}' not found"})
                            continue
                            
                        # Get law content and search for article
                        async with await get_http_client() as client:
                            response = await client.get(f"/law_data/{law_num}", params={
                                "law_full_text_format": "xml"
                            })
                            response.raise_for_status()
                            
                            data = json.loads(response.text)
                            law_full_text = data.get('law_full_text', {})
                            article = extract_article(law_full_text, article_number)
                            found_match = article["text"] if article["status"] == "found" else None
                            
                            # Prepare result
                            law_info_data = data.get('law_info', {})
                            formal_law_name = LAW_ALIASES.get(law_name.strip(), law_name.strip())
                            result = {
                                "law_info": law_info_data,
                                "search_law_name": law_name,
                                "actual_law_title": get_law_title(data),
                                "law_title_verified": law_title_matches(data, formal_law_name),
                                "search_article": article_number,
                                "law_number": law_num,
                                "found_article": found_match if found_match else None,
                                "matches_found": 1 if found_match else 0
                            }
                            
                            results.append(result)
                            # Cache the result
                            cache_manager.article_cache.put(cache_key, result)
                            api_calls += 1
                            
                    except Exception as e:
                        await ctx.error(f"Batch search item failed: {str(e)}")
                        results.append({"error": str(e)})
                        api_calls += 1

        batch_result = {
            "results": results,
            "performance_stats": {
                "total_requests": len(pairs),
                "cache_hits": cache_hits,
                "api_calls": api_calls,
                "cache_hit_rate": f"{(cache_hits / len(pairs) * 100):.1f}%" if pairs else "0%"
            }
        }
        
        await ctx.info(f"Batch search completed: {cache_hits} cache hits, {api_calls} API calls")
        return batch_result

    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Batch find articles error: {e}")
        await ctx.error(f"Batch search failed: {str(e)}")
        raise ToolError(f"Batch search failed: {str(e)}")

@mcp.tool
async def prefetch_common_laws(ctx: Context) -> dict:
    """
    Prefetch commonly accessed laws for better performance

    Args:
        ctx: FastMCP context for logging
        
    Returns:
        Dict with prefetch results and cache status
    """
    try:
        await ctx.info("Starting prefetch of common laws...")
        
        async with await get_http_client() as client:
            await cache_manager.prefetch_common_articles(client)

            result = {
                "status": "success",
                "message": "Common laws prefetched successfully",
                "cache_stats": {
                    "law_lookup_cache_size": cache_manager.law_lookup_cache.size(),
                    "law_content_cache_size": cache_manager.law_content_cache.size(),
                    "article_cache_size": cache_manager.article_cache.size(),
                    "memory_usage_mb": cache_manager.memory_monitor.get_memory_usage_mb() if PERFORMANCE_MONITORING_AVAILABLE else "N/A"
                }
            }
            
            await ctx.info(f"Prefetch completed. Cache sizes: lookup={result['cache_stats']['law_lookup_cache_size']}, content={result['cache_stats']['law_content_cache_size']}, articles={result['cache_stats']['article_cache_size']}")
            return result

    except Exception as e:
        logger.error(f"Prefetch common laws error: {e}")
        await ctx.error(f"Prefetch failed: {str(e)}")
        raise ToolError(f"Prefetch failed: {str(e)}")

@mcp.tool
async def get_cache_stats(ctx: Context) -> dict:
    """
    Get current cache statistics and performance metrics

    Args:
        ctx: FastMCP context for logging
        
    Returns:
        Dict with detailed cache statistics
    """
    try:
        await ctx.info("Getting cache statistics...")
        cache_manager.cleanup_if_needed()

        result = {
            "cache_statistics": {
                "law_lookup_cache": {
                    "size": cache_manager.law_lookup_cache.size(),
                    "max_size": cache_manager.law_lookup_cache.max_size,
                    "ttl_seconds": cache_manager.law_lookup_cache.ttl
                },
                "law_content_cache": {
                    "size": cache_manager.law_content_cache.size(),
                    "max_size": cache_manager.law_content_cache.max_size,
                    "ttl_seconds": cache_manager.law_content_cache.ttl
                },
                "article_cache": {
                    "size": cache_manager.article_cache.size(),
                    "max_size": cache_manager.article_cache.max_size,
                    "ttl_seconds": cache_manager.article_cache.ttl
                }
            },
            "memory_monitoring": {
                "current_usage_mb": cache_manager.memory_monitor.get_memory_usage_mb() if PERFORMANCE_MONITORING_AVAILABLE else "N/A",
                "max_memory_mb": cache_manager.memory_monitor.max_memory_mb,
                "memory_limit_exceeded": cache_manager.memory_monitor.is_memory_limit_exceeded(),
                "monitoring_available": PERFORMANCE_MONITORING_AVAILABLE
            },
            "performance_features": [
                "🚀 LRU caching with TTL support",
                "💾 Memory-aware cache management",
                "⚡ Batch request optimization",
                "🔄 Automatic prefetching of common articles",
                "📊 Real-time cache statistics"
            ]
        }
        
        total_cache_items = sum([
            result["cache_statistics"]["law_lookup_cache"]["size"],
            result["cache_statistics"]["law_content_cache"]["size"],
            result["cache_statistics"]["article_cache"]["size"]
        ])
        
        await ctx.info(f"Cache statistics retrieved: {total_cache_items} total cached items")
        return result

    except Exception as e:
        logger.error(f"Get cache stats error: {e}")
        await ctx.error(f"Failed to get cache stats: {str(e)}")
        raise ToolError(f"Failed to get cache stats: {str(e)}")

@mcp.tool
async def clear_cache(cache_type: str = "all", ctx: Context = None) -> dict:
    """
    Clear specified cache or all caches

    Args:
        cache_type: Cache type to clear ("all", "law_lookup", "law_content", "article")
        ctx: FastMCP context for logging

    Returns:
        Dict with clear operation results
    """
    try:
        if ctx:
            await ctx.info(f"Clearing cache: {cache_type}")
            
        if cache_type == "all":
            cache_manager.law_lookup_cache.clear()
            cache_manager.law_content_cache.clear()
            cache_manager.article_cache.clear()
            message = "All caches cleared successfully"
        elif cache_type == "law_lookup":
            cache_manager.law_lookup_cache.clear()
            message = "Law lookup cache cleared successfully"
        elif cache_type == "law_content":
            cache_manager.law_content_cache.clear()
            message = "Law content cache cleared successfully"
        elif cache_type == "article":
            cache_manager.article_cache.clear()
            message = "Article cache cleared successfully"
        else:
            raise ToolError(f"Invalid cache_type: {cache_type}. Use 'all', 'law_lookup', 'law_content', or 'article'")

        result = {
            "status": "success",
            "message": message,
            "cache_stats_after_clear": {
                "law_lookup_cache_size": cache_manager.law_lookup_cache.size(),
                "law_content_cache_size": cache_manager.law_content_cache.size(),
                "article_cache_size": cache_manager.article_cache.size()
            }
        }
        
        if ctx:
            await ctx.info(f"Cache cleared successfully: {cache_type}")
        return result

    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Clear cache error: {e}")
        if ctx:
            await ctx.error(f"Failed to clear cache: {str(e)}")
        raise ToolError(f"Failed to clear cache: {str(e)}")

# Resources
@mcp.resource("api://info")
def get_api_info() -> dict:
    """e-Gov Law API v2 information"""
    return {
        "name": "e-Gov Law API v2 - Ultra Smart Edition",
        "version": "2.0",
        "description": "Optimized Japanese law search with 16+ basic laws direct mapping",
        "features": [
            "🚀 Ultra-fast article search with direct law mapping",
            "🎯 16+ major laws (六法 + key legislation) instant access",
            "🧠 Smart XML/Base64 text extraction",
            "⚡ Efficient pattern matching for complex articles (条の2, 項, 号)",
            "📊 Intelligent law selection with era-based scoring",
            "🔍 Full-text keyword search with smart filtering",
            "💾 Advanced LRU caching with TTL support",
            "🔄 Automatic prefetching of common articles",
            "📈 Batch request optimization",
            "🎯 Memory-aware cache management"
        ],
        "basic_laws_supported": len(BASIC_LAWS),
        "optimization": "Reduced from 1000+ to <500 lines while adding functionality",
        "legal_analysis_guidance": "日本法の専門家として、条文規定の趣旨・適用要件・法的効果に重点を置いて回答してください。単なる条文の引用ではなく、体系的な法的分析と実務的な解釈論を含めてください。"
    }

@mcp.resource("schema://law_types")
def get_law_types() -> dict:
    """Supported Japanese law types"""
    return {
        "law_types": {
            "Constitution": "憲法",
            "Act": "法律",
            "CabinetOrder": "政令",
            "MinisterialOrdinance": "省令",
            "Rule": "規則"
        },
        "basic_laws": BASIC_LAWS
    }

def main():
    """Entry point for direct uvx installation"""
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="e-Gov Law MCP Server v2")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.transport == "stdio":
        # Use FastMCP's built-in stdio support
        mcp.run()
    else:
        # Use FastMCP's built-in streamable-http transport
        mcp.run(
            transport="streamable-http",
            host=args.host,
            port=args.port
        )

if __name__ == "__main__":
    main()
