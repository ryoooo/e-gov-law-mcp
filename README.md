# e-Gov Law MCP Server v2 🏛️⚖️

[![FastMCP](https://img.shields.io/badge/FastMCP-Compatible-green)](https://github.com/jlowin/fastmcp)
[![Python](https://img.shields.io/badge/Python-3.8%2B-blue)](https://www.python.org)
[![Test Coverage](https://img.shields.io/badge/Coverage-65%25-brightgreen)](https://github.com/ryoooo/e-gov-law-mcp/actions)
[![Windows](https://img.shields.io/badge/Windows-Compatible-blue)](https://github.com/ryoooo/e-gov-law-mcp)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

**Ultra Smart & Efficient** - 日本政府e-Gov法令APIのための高性能Model Context Protocol (MCP) サーバー

> **🚀 v2の特徴**: FastMCP準拠、Windows完全対応、58%コード削減、3層キャッシュ最適化、エンタープライズセキュリティ

## ✨ 主な特徴

### 🎯 **インテリジェント法律検索**
- **16基本法直接マッピング**: 六法 + 現代重要法への瞬時アクセス
- **20略称自動変換**: 道交法→道路交通法、労基法→労働基準法
- **複雑パターン対応**: 「第325条の3」「第9条第2項第1号」等
- **4段階条文抽出**: コンテンツスコアリングによる高精度抽出

### ⚡ **ハイパフォーマンス**
- **3層LRUキャッシュ**: 法律検索(2h)、法律内容(1h)、条文(30m)
- **並行処理最適化**: 50リクエスト/5.27秒の高速レスポンス
- **メモリ監視**: psutil統合、自動クリーンアップ（512MB制限）
- **バッチ処理**: 最大200件の一括検索対応

### 🛡️ **エンタープライズセキュリティ**
- **インジェクション防止**: SQL、XSS、JNDI、コード実行を完全ブロック
- **入力検証**: 長さ制限、特殊文字フィルタリング
- **API保護**: レート制限、403 Forbidden応答
- **エラーマスキング**: 内部情報漏洩防止

### 🌐 **クロスプラットフォーム**
- **Windows完全対応**: psutilオプション、パス互換性
- **FastMCP準拠**: Context logging、ToolError例外、自動シリアライゼーション
- **柔軟設定**: YAML設定ファイル、プロンプト外部化

## 🛠️ 8つの高機能MCPツール

| ツール | 機能 | 特徴 |
|--------|------|------|
| `find_law_article` | 条文検索 | AI駆動パターンマッチング、漢数字対応 |
| `search_laws` | 法律検索 | フィルタリング、ページネーション |
| `search_laws_by_keyword` | キーワード検索 | フルテキスト検索、ハイライト |
| `get_law_content` | 法律全文取得 | サイズ制限対応（800KB）、XML/JSON |
| `batch_find_articles` | バッチ検索 | 最大200件、パフォーマンス統計 |
| `prefetch_common_laws` | キャッシュ最適化 | 頻出法律の事前読み込み |
| `get_cache_stats` | 監視 | リアルタイムパフォーマンス監視 |
| `clear_cache` | メンテナンス | 粒度別キャッシュ管理 |

## 📊 対応法令

### 🚀 高速アクセス対応（直接マッピング済み）

**六法**
- 憲法（昭和二十一年憲法）
- 民法（明治二十九年法律第八十九号）
- 刑法（明治四十年法律第四十五号）
- 商法（明治三十二年法律第四十八号）
- 民事訴訟法（平成八年法律第百九号）
- 刑事訴訟法（昭和二十三年法律第百三十一号）

**現代重要法**
- 会社法、労働基準法、所得税法、法人税法
- 著作権法、特許法、道路交通法、建築基準法
- 独占禁止法、消費者契約法、特定受託事業者取引適正化法

**税法**
- 消費税法、相続税法、地方税法、国税通則法、租税特別措置法

### 🔎 その他の法令（題名検索）

直接マッピングにない法令は、e-Gov API の題名検索で探します。題名が完全に一致する法令を優先して選ぶため、
題名にその法令名を含むだけの改正法・特例法（例:「…消費税法の一部を改正する等の法律」）は選ばれません。
法律に一致するものがなければ、政令・省令なども探します（例: 消費税法施行令）。
取得した法令の題名は `actual_law_title` で返し、指定した法令名と一致しない場合は
`law_title_verified: false` と `law_title_warning` を付けます。

### 🔍 略称検索対応
```
道交法 → 道路交通法    労基法 → 労働基準法
独禁法 → 独占禁止法    消契法 → 消費者契約法
著作権 → 著作権法      特許 → 特許法
税法 → 所得税法        労働法 → 労働基準法
```

## 📈 実証された性能

### 🏆 テスト結果（40+テスト、65%カバレッジ）
- ✅ **機能テスト**: 全ツール、エッジケース、境界値
- ✅ **セキュリティテスト**: インジェクション攻撃、不正ペイロード
- ✅ **パフォーマンステスト**: 並行性50/50成功、5.27秒
- ✅ **統合テスト**: FastMCP機能、Windows互換性

### 📊 パフォーマンス指標
- **並行処理**: 50リクエスト同時処理 → 100%成功
- **キャッシュヒット率**: 主要法律で90%以上
- **レスポンス時間**: 直接マッピング法律 < 1秒
- **メモリ効率**: 512MB制限内で安定動作

## 🚀 クイックスタート

### 前提条件
[uv](https://docs.astral.sh/uv/)をインストール：

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### インストール

```bash
# リポジトリをクローン
git clone https://github.com/ryoooo/e-gov-law-mcp.git
cd e-gov-law-mcp

# 依存関係をインストール
uv sync

# パフォーマンス監視を有効にする場合（オプション）
uv add psutil
```

### Claude Desktop設定

設定ファイルの場所：
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Linux**: `~/.config/claude/claude_desktop_config.json`

**設定例**:
```json
{
  "mcpServers": {
    "e-gov-law": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/e-gov-law-mcp",
        "python",
        "run_server.py"
      ]
    }
  }
}
```

## 💡 使用例

### Claude Desktopでの基本使用
```
民法192条について詳しく教えて
憲法第9条第2項の条文と解釈を知りたい
会社法325条の3の株主総会決議について
労基法の有給休暇の規定を調べて
道交法の飲酒運転の罰則は？
```

### プログラム使用例
```python
import asyncio
from fastmcp import Client

async def search_example():
    async with Client(["uv", "run", "python", "src/mcp_server.py"]) as client:
        # 民法192条を検索
        result = await client.call_tool("find_law_article", {
            "law_name": "民法",
            "article_number": "192"
        })
        print(result[0].text)

        # バッチ検索
        batch_data = json.dumps([
            {"law": "民法", "article": "192"},
            {"law": "憲法", "article": "9"}
        ])
        batch_result = await client.call_tool("batch_find_articles", {
            "law_article_pairs": batch_data
        })

asyncio.run(search_example())
```

## 🔧 開発とテスト

### 開発環境セットアップ
```bash
git clone https://github.com/ryoooo/e-gov-law-mcp.git
cd e-gov-law-mcp
uv sync --dev
```

### テスト実行
```bash
# 全テスト実行（40+テスト）
uv run pytest

# カバレッジ付きテスト
uv run pytest --cov=src

# セキュリティテスト
uv run pytest test_comprehensive_ultra.py::TestSecurityAndRobustness -v

# パフォーマンステスト
uv run pytest test_comprehensive_ultra.py::TestPerformanceAndScalability -v

# FastMCP統合テスト
uv run pytest test_fastmcp_integration.py -v
```

### コード品質
```bash
# フォーマット
uv run black src/ tests/

# リント
uv run ruff check src/ tests/

# 型チェック
uv run mypy src/
```

## ⚙️ 設定

### 環境変数
```bash
# e-Gov API設定
export EGOV_API_URL="https://laws.e-gov.go.jp/api/2"
export EGOV_API_TOKEN=""  # 通常は不要

# サーバー設定
export MCP_SERVER_NAME="e-Gov Law Server v2"
export LAW_CONFIG_PATH="config/laws.yaml"
```

### 設定ファイル
- `config/laws.yaml`: 法律マッピング、略称定義
- `prompts/legal_analysis.md`: 法的分析指導プロンプト

## 🏗️ アーキテクチャ

```
e-Gov Law MCP Server v2
├── 🎯 FastMCP Core (Context logging, ToolError, Auto-serialization)
├── 🔍 Smart Law Lookup (16 basic laws + 20 aliases)
├── ⚡ 3-Tier Cache System (LRU + TTL + Memory monitoring)
├── 🛡️ Security Layer (Injection prevention, Input validation)
├── 🌐 Cross-Platform Support (Windows/Linux/macOS)
└── 📊 Performance Monitoring (Real-time stats, Batch optimization)
```

## 📝 API詳細

### find_law_article
最も重要なツール - 高精度条文検索

```python
await client.call_tool("find_law_article", {
    "law_name": "民法",           # 法律名（略称可）
    "article_number": "325条の3"  # 条文番号（複雑パターン対応）
})
```

**対応パターン例**:
- `"192"` → 第192条
- `"第192条"` → 第192条  
- `"325条の3"` → 第325条の3
- `"第9条第2項"` → 第9条第2項
- `"第9条第2項第1号"` → 第9条第2項第1号
- `"第2条第1項第3号の2"` → 第2条第1項第3号の2
- `"第九条"` → 第9条

条文は法令 XML の本則（MainProvision）から取り出すので、目次や附則の同じ番号の条は返しません。
附則の条と、号の細目（イ、ロ…）は指定できません（号まで指定してください）。

### batch_find_articles
高速バッチ処理 - 最大200件の一括検索

```python
batch_data = json.dumps([
    {"law": "民法", "article": "192"},
    {"law": "憲法", "article": "9"},
    {"law": "会社法", "article": "423"}
])

await client.call_tool("batch_find_articles", {
    "law_article_pairs": batch_data
})
```

## 🔧 トラブルシューティング

### Windows環境トラブルシューティング

**問題: "No module named 'yaml'" エラー**
```bash
# 解決方法1: uv syncで依存関係をインストール
cd C:\path\to\e-gov-law-mcp
uv sync

# 解決方法2: 手動インストール
pip install PyYAML httpx fastmcp
```

**問題: "No module named 'psutil'" 警告**
```bash
# psutilはオプションのパフォーマンス監視ライブラリ
# インストールしなくても動作します

# パフォーマンス監視を有効にしたい場合
uv add psutil
# または
pip install psutil
```

**問題: FastMCPコマンドエラー**
```bash
# Claude Desktop設定でrun_server.pyを使用
# 依存関係チェック機能付き
"command": "uv",
"args": [
  "run",
  "--directory",
  "C:\\path\\to\\e-gov-law-mcp",
  "python",
  "run_server.py"
]
```

**その他のWindows問題**
```bash
# パス区切り文字 → pathlibで自動解決
# UTF-8エンコーディング → 明示的指定済み
# メモリ監視 → psutilなしでも動作
```

### パフォーマンス最適化
```bash
# キャッシュ統計確認
await client.call_tool("get_cache_stats", {})

# 頻出法律をプリフェッチ
await client.call_tool("prefetch_common_laws", {})

# キャッシュクリア（メモリ不足時）
await client.call_tool("clear_cache", {"cache_type": "all"})
```

## 📚 参考リンク

- [FastMCP Documentation](https://github.com/jlowin/fastmcp)
- [e-Gov法令API v2 仕様書](https://laws.e-gov.go.jp/api/2/swagger-ui/)
- [Model Context Protocol](https://docs.anthropic.com/en/docs/build-with-claude/mcp)
- [プロジェクト技術文書](CLAUDE.md)

## 🤝 貢献

1. リポジトリをフォーク
2. 機能ブランチを作成: `git checkout -b feature/amazing-feature`
3. 変更をコミット: `git commit -m 'feat: Add amazing feature'`
4. ブランチにプッシュ: `git push origin feature/amazing-feature`
5. プルリクエストを作成

### 貢献ガイドライン
- テストカバレッジを維持（65%以上）
- セキュリティテストを必須追加
- FastMCP仕様準拠を確認
- Windows互換性をテスト

## 📄 ライセンス

MIT License - 詳細は [LICENSE](LICENSE) ファイルを参照

## 🙏 謝辞

- [e-Gov](https://www.e-gov.go.jp/) - 法令API提供
- [FastMCP](https://github.com/jlowin/fastmcp) - MCPフレームワーク
- [Anthropic](https://www.anthropic.com/) - MCP仕様策定

---

**🚀 Ultra Smart & Efficient e-Gov Law MCP Server v2**  
*日本法令検索の新しいスタンダード*