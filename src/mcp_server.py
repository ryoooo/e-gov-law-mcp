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
from typing import Any, Optional

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
            extracted_text = extract_text_from_xml(law_full_text)

            # Step 3: Smart article search
            patterns = generate_search_patterns(article_number)
            matches = []

            for pattern in patterns:
                # Enhanced text extraction for complete articles
                article_pattern = re.escape(pattern)

                # Find all matches first and filter for actual content vs table of contents
                all_matches = []

                # Find all occurrences of the pattern
                for match in re.finditer(article_pattern, extracted_text):
                    pos = match.start()

                    # Check if this is the START of an actual article (not a reference)
                    # Look at context before the match
                    context_before = extracted_text[max(0, pos-50):pos]
                    context_after = extracted_text[pos:pos+100]

                    # Skip if this appears to be a reference within another article
                    if re.search(r'第\d+条.*第\d+条', context_before + context_after):
                        continue  # This is likely a reference, not the actual article start

                    # Look for patterns that indicate this is an actual article start
                    is_actual_article = False

                    # Pattern 1: Article number followed by title/content structure
                    if re.search(rf'{article_pattern}\s*\n\s*\n\s*\n\s*', extracted_text[pos:pos+200]):
                        is_actual_article = True

                    # Pattern 2: Article number at start of line with proper indentation
                    if context_before.endswith('\n              ') or context_before.endswith('            '):
                        is_actual_article = True

                    # Pattern 3: Article number followed by paragraph structure
                    if re.search(rf'{article_pattern}\s*\n.*?\n.*?\n.*?[あ-ん]', extracted_text[pos:pos+300], re.DOTALL):
                        is_actual_article = True

                    if not is_actual_article:
                        continue

                    # Try multiple extraction strategies for this position
                    strategies = [
                        # Strategy 1: Article to next kanji article number
                        f"{article_pattern}.*?(?=第[一二三四五六七八九十百千]+条)",

                        # Strategy 2: Article excluding next "第" character
                        f"{article_pattern}[^第]*",

                        # Strategy 3: Article to title pattern + next article
                        f"{article_pattern}.*?(?=（[^）]*）\\s*第)",

                        # Strategy 4: Fixed character limit
                        f"{article_pattern}.{{0,2000}}"
                    ]

                    # Get context around this match
                    context_start = max(0, pos - 20)
                    context_end = min(len(extracted_text), pos + 3000)
                    context = extracted_text[context_start:context_end]

                    for strategy in strategies:
                        matches_found = re.findall(strategy, context, re.DOTALL | re.MULTILINE)
                        if matches_found:
                            candidate = matches_found[0].strip()
                            if len(candidate) > 50:
                                # Score this candidate based on content quality
                                content_score = 0

                                # Heavily favor actual article content patterns
                                content_score += 10  # Base score for being an actual article

                                # Prefer longer content
                                if len(candidate) > 200: content_score += 3
                                elif len(candidate) > 100: content_score += 2

                                # Prefer content with sentence endings
                                if '。' in candidate: content_score += 3

                                # Prefer content with commas (actual text)
                                if '、' in candidate: content_score += 2

                                # Prefer content with hiragana (actual content vs table)
                                if re.search(r'[あ-ん]+', candidate): content_score += 3

                                # Penalize reference patterns
                                if '―' in candidate: content_score -= 5
                                if candidate.count('第') > 3: content_score -= 2  # Too many references

                                all_matches.append((content_score, candidate, pos))
                                break

                # Sort by content score (highest first) and take best matches
                all_matches.sort(key=lambda x: x[0], reverse=True)

                # Process top scored matches
                for score, candidate, pos in all_matches[:3]:  # Take top 3 candidates
                    if score > 10:  # Only accept high-quality actual articles
                        clean_match = candidate.strip()
                        # Accept matches that look like complete articles
                        if len(clean_match) > 30 and clean_match not in matches:
                            # Ensure we have a complete sentence/clause ending
                            if clean_match.endswith(('。', '）', '）。', '号', '項', '条')):
                                matches.append(clean_match)
                            else:
                                # Try to find a good stopping point
                                for ending in ['。', '）。', '号。', '項。']:
                                    if ending in clean_match:
                                        last_pos = clean_match.rfind(ending)
                                        if last_pos > len(clean_match) * 0.7:  # Must be in latter part
                                            truncated = clean_match[:last_pos + len(ending)]
                                            matches.append(truncated)
                                            break
                                else:
                                    # If no good ending found, use as-is if substantial
                                    if len(clean_match) > 100:
                                        matches.append(clean_match)

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
                # Smart suggestions for missing articles
                main_num = re.search(r'(\d+)', article_number)
                if main_num:
                    article_num = main_num.group(1)
                    kanji_num = arabic_to_kanji(article_num)
                    basic_patterns = [f"第{kanji_num}条", f"第{article_num}条"]

                    basic_found = any(re.search(re.escape(p), extracted_text) for p in basic_patterns)

                    if basic_found:
                        if 'の' in article_number:
                            result["suggestion"] = f"Article {article_num} exists, but the specified 'の' variation may not exist."
                        elif '項' in article_number or '号' in article_number:
                            result["suggestion"] = f"Article {article_num} exists, but the specified paragraph (項) or subparagraph (号) may not exist."
                        else:
                            result["suggestion"] = f"Article found with different formatting. Try searching for just '{article_num}'."
                    else:
                        result["suggestion"] = f"Article {article_number} not found in {law_name}. Please verify the article number."

                result["search_patterns_used"] = patterns[:5]

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
                            extracted_text = extract_text_from_xml(law_full_text)
                            
                            # Simple article search for batch processing
                            patterns = generate_search_patterns(article_number)
                            found_match = None
                            
                            for pattern in patterns[:3]:  # Use only first 3 patterns for speed
                                article_pattern = re.escape(pattern)
                                matches = re.findall(f"{article_pattern}.{{0,500}}", extracted_text, re.DOTALL)
                                if matches:
                                    found_match = matches[0].strip()
                                    break
                            
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
