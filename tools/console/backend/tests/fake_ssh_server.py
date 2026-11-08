"""テスト専用の模擬 SSH サーバー（本番サーバーに触らずにバックエンドを確かめるためのもの）。

本物の SSH プロトコル（鍵認証・ホスト鍵の検証・コマンド実行・ストリーム）で通信し、
systemctl / journalctl / free / git / pip などの「出力だけ」を作り物で返す。
ただし bot.db を読むスクリプト（DB の集計・テーブルの中身）は、架空の bot.db に対して本当に実行する。
画面の接続先には出さない（利用者の指定で、開発用の接続先としては使わない）。自動テストだけが使う。

start_fake_server(...) に渡した一時フォルダへ、次のファイルを作る:
- host_key          … このサーバーのホスト鍵
- client_key(.pub)  … ダッシュボードがログインに使う鍵
- known_hosts       … ダッシュボードがホスト鍵を検証するためのファイル
- bot/data/memory/bot.db・bot/data/ng/blacklist.json … 架空の利用者・会話で作った DB と違反記録（make_fake_database）

デプロイの確かめ方: bot.repo（FakeRepo）で、新しいコミット・requirements.txt の変更・pip の失敗・
起動に失敗する版などを作ってから、デプロイを実行する。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncssh

SERVICE_NAME = "bot"

# 実際のボットが出すログに似せた文言（重み, 本文）
LIVE_LOG_TEMPLATES: list[tuple[int, str]] = [
    (14, "Gemini: 応答を生成しました（{model} / {sec:.2f}秒 / 入力 {tin:,} tok / 出力 {tout:,} tok）"),
    (6, "記憶圧縮: ユーザー 1 人分の pending_log {n} 件を profile へ統合しました。"),
    (5, "自発的な会話: #雑談 へ話題を投稿しました（無会話 6 時間）"),
    (4, "リマインダー: {n} 件を通知しました。"),
    (4, "文書処理: report_{n}.pdf の要約が完了しました（{sec:.1f}秒）"),
    (3, "自律行動: ステージ「雑談ステージ」で配信を開始しました"),
    (3, "自律行動: 配信を終了しました（理由: コメントが30分途絶えたため）"),
    (3, "TTS: edge-tts に失敗したため gTTS で読み上げます（ConnectionResetError）"),
    (2, "TTS: edge-tts が復旧しました。通常の読み上げに戻ります。"),
    (2, "自発的な会話: 話題の生成に失敗しました。次回の見回りで再試行します。"),
    (2, "警告: モデレーションがタイムアウトしたため、安全として扱います。"),
    (2, "ルーティング判定エラー: 503 UNAVAILABLE"),
    (1, "バックグラウンド処理エラー (stats.record): TimeoutError: "),
]
STARTUP_LOGS = [
    "データベース: スキーマは最新です（バージョン 10）",
    "ログイン成功: PersonaBot#0000",
    "スラッシュコマンドを同期しました（3 件）",
    "見回りループを開始しました（記憶 / 自発会話 / 自律行動 / 記念日 / リマインダー / 評価）",
    "文書ワーカーを起動しました。",
]
TRACEBACK_LOGS = [
    "Traceback (most recent call last):",
    '  File "/home/ubuntu/Persona-Chat-in-Discord/src/persona_bot/services/gemini_service.py", line 212, in generate',
    "google.genai.errors.ServerError: 503 UNAVAILABLE. The model is overloaded.",
]
# 会話1件ぶんのログ（ボットの usage_graph.log_snapshot と同じ形。応答の2行目以降は journald では別の行になる）。
# 中身は架空の会話。画面の伏せ字モード（会話の本文を隠す）を確かめるために出す
CONVERSATIONS = [
    ("直接会話", "明日の天気って分かる？", "画像添付: 0枚", ["明日は晴れのち曇りみたい。", "夕方から風が強くなるから、上着があると安心だよ。"]),
    ("直接会話", "おすすめの本を教えて", "画像添付: 0枚", ["最近だと短編集がおすすめ。", "通学の合間にも読みやすいよ。", "気になったら感想を聞かせてね。"]),
    ("自律行動/独り言", "配信中の独り言", "自動実行", ["今日は誰か来てくれるかな。"]),
]
SEPARATOR_LINE = "-" * 124

MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-2.5-flash"]


def logging_line(message: str, *, logger: str = "persona_bot.core.bot_client", at: float | None = None) -> str:
    """新しい標準出力の書式を、実際の logging 設定を変更せず再現する。"""
    stamp = datetime.fromtimestamp(at if at is not None else time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    return f"[{stamp}] [INFO] [{logger}] {message}"


def fake_commit(number: int) -> str:
    """作り物のコミット ID（40 桁の16進数）。"""
    return hashlib.sha1(f"fake-commit-{number}".encode()).hexdigest()


@dataclass
class FakeRepo:
    """模擬サーバーの中の「ボットのフォルダ（git）」の状態。history は origin/main のコミットを古い順に並べたもの。"""

    branch: str = "main"
    history: list[tuple[str, str]] = field(
        default_factory=lambda: [
            (fake_commit(1), "fix: 会話要約の失敗表示を修正"),
            (fake_commit(2), "feat: 開発者用ダッシュボードを追加"),
            (fake_commit(3), "fix: 読み上げの音色を修正"),
            (fake_commit(4), "feat: リマインダーの繰り返しを追加"),
        ]
    )
    head_index: int = 1
    dirty: int = 0
    # この番号のコミットで requirements.txt が変わる（取り込むと、依存関係を入れ直す）
    requirements_changed_at: set[int] = field(default_factory=set)
    # この番号のコミットで constraints.txt（推移依存の固定版）が変わる（同じく入れ直す）
    constraints_changed_at: set[int] = field(default_factory=set)
    # この版で起動すると、ログインの前に例外で止まる
    broken: set[str] = field(default_factory=set)
    # この版の requirements.txt は入れられない（pip install が失敗する）
    pip_fails_at: set[str] = field(default_factory=set)
    fetch_fails: bool = False
    reset_fails: bool = False
    commands: list[str] = field(default_factory=list)

    @property
    def head(self) -> str:
        return self.history[self.head_index][0]

    @property
    def origin(self) -> str:
        return self.history[-1][0]

    def index_of(self, commit: str) -> int:
        return next(index for index, (sha, _) in enumerate(self.history) if sha == commit)

    def status_output(self) -> str:
        """概要ページの「デプロイ中のコード」（remote_commands.git_command）の出力。"""
        sha, subject = self.history[self.head_index]
        return f"{self.branch}\n{sha[:7]}\x1f{subject}\x1f2026-10-16T21:14:05+09:00\nver0.1.7-12-g{sha[:7]}\n{self.dirty}\n"

    def respond_rollback(self, command: str) -> tuple[str, str, int] | None:
        """remote_commands.deploy_rollback_command（sh -c '…'）の意味をなぞる。

        戻す直前にブランチ・HEAD・追跡ファイルの変更を確かめ、想定と違えば何も変えずに止まる（終了コード 10・11・12）。
        本物の git での挙動は test_deploy.RollbackCommandRealGitTests が確かめている。
        """
        script = shlex.split(command)[2]
        branch = re.search(r'\[ "\$branch" = (\S+) \]', script)
        reset = re.search(r"git reset --keep ([0-9a-f]{40}) && git rev-parse HEAD$", script)
        cases = re.search(r'case "\$head" in (.*) esac', script)
        if not (branch and reset and cases):
            return None
        commit = reset.group(1)
        allowed = set(re.findall(r"[0-9a-f]{40}", cases.group(1)))
        if self.branch != shlex.split(branch.group(1))[0]:
            return f"{self.branch}\n", "", 10
        if self.head == commit:
            return f"{self.head}\n", "", 0
        if self.head not in allowed:
            return f"{self.head}\n", "", 11
        if self.dirty:
            return f"{self.dirty}\n", "", 12
        if self.reset_fails:
            return "", "fatal: Unable to create '.git/index.lock': File exists.\n", 128
        self.head_index = self.index_of(commit)
        return f"{self.head}\n", "", 0

    def respond(self, command: str) -> tuple[str, str, int] | None:
        """「cd <ボットのフォルダ> && ...」の後ろの git / pip のコマンドに答える。知らないコマンドなら None。"""
        self.commands.append(command)
        if command == "git rev-parse --abbrev-ref HEAD && git rev-parse HEAD && git status --porcelain --untracked-files=no | wc -l":
            return f"{self.branch}\n{self.head}\n{self.dirty}\n", "", 0
        fetch = re.fullmatch(
            r"git fetch --quiet origin \+refs/heads/(\S+):refs/remotes/origin/\1"
            r" && git rev-parse refs/remotes/origin/\1"
            r" && git rev-list --count HEAD\.\.refs/remotes/origin/\1"
            r" && git rev-list --count refs/remotes/origin/\1\.\.HEAD",
            command,
        )
        if fetch:
            if self.fetch_fails or fetch.group(1) != self.branch:
                return "", f"fatal: couldn't find remote ref {fetch.group(1)}\n", 128
            return f"{self.origin}\n{len(self.history) - 1 - self.head_index}\n0\n", "", 0
        if re.fullmatch(r"git merge --ff-only refs/remotes/origin/\S+ && git rev-parse HEAD", command):
            before = self.head
            self.head_index = len(self.history) - 1
            return f"Updating {before[:7]}..{self.head[:7]}\nFast-forward\n{self.head}\n", "", 0
        if match := re.fullmatch(r"git log --format=%h%x1f%s ([0-9a-f]{40})\.\.([0-9a-f]{40})", command):
            start, end = self.index_of(match.group(1)), self.index_of(match.group(2))
            lines = [f"{sha[:7]}\x1f{subject}" for sha, subject in reversed(self.history[start + 1 : end + 1])]
            return "".join(f"{line}\n" for line in lines), "", 0
        if match := re.fullmatch(
            r"git diff --name-only ([0-9a-f]{40}) ([0-9a-f]{40}) -- requirements.txt constraints.txt", command
        ):
            start, end = sorted((self.index_of(match.group(1)), self.index_of(match.group(2))))
            span = range(start + 1, end + 1)
            names = [
                name
                for name, changed_at in (
                    ("requirements.txt", self.requirements_changed_at),
                    ("constraints.txt", self.constraints_changed_at),
                )
                if any(index in changed_at for index in span)
            ]
            return "".join(f"{name}\n" for name in names), "", 0
        if re.fullmatch(r"\S+ -m pip install --disable-pip-version-check --no-input -r requirements.txt", command):
            if self.head in self.pip_fails_at:
                return "", "ERROR: Could not find a version that satisfies the requirement example-package==9.9.9\n", 1
            return "Successfully installed example-package-1.2.3\n", "", 0
        if re.fullmatch(r"\S+ -m pip check", command):
            return "No broken requirements found.\n", "", 0
        if command.startswith("sh -c "):
            return self.respond_rollback(command)
        if command.startswith("git rev-parse --abbrev-ref HEAD && git log -1"):
            return self.status_output(), "", 0
        return None


@dataclass
class FakeBot:
    """模擬サーバーの中の「ボット」の状態。"""

    repo: FakeRepo = field(default_factory=FakeRepo)
    # 架空の bot.db と違反記録（make_fake_database が作る。DB を読むスクリプトはこれに対して本当に実行する）
    db_path: Path | None = None
    blacklist_path: Path | None = None
    # 再起動などの待ち時間の倍率（テストを速くするため小さくする）
    delay_scale: float = 1.0
    failed: bool = False
    active: bool = True
    pid: int = 41237
    restarts: int = 2
    memory_bytes: float = 182_000_000
    host_memory_used: float = 610_000_000
    boot_mono: float = field(default_factory=lambda: time.monotonic() - 9 * 86400 - 3600 * 5)
    started_mono: float = field(default_factory=lambda: time.monotonic() - 2 * 86400 - 3600 * 3 - 60 * 17)
    cpu_seconds_base: float = 5321.0
    stopped_at: float | None = None
    gemini_calls: int = 1342
    gemini_tokens: int = 1_820_000
    entries: list[dict] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    connections: set[asyncssh.SSHServerConnection] = field(default_factory=set)
    _busy: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ---------------------------------------------------------- ログ

    def log(self, message: str, *, source: str = "python", priority: int = 6, at: float | None = None) -> None:
        entry = {
            "__REALTIME_TIMESTAMP": str(int((at if at is not None else time.time()) * 1_000_000)),
            "PRIORITY": str(priority),
            "_PID": str(self.pid if source == "python" else 1),
            "SYSLOG_IDENTIFIER": source,
            "_SYSTEMD_UNIT": f"{SERVICE_NAME}.service",
            "MESSAGE": message,
        }
        self.entries.append(entry)
        del self.entries[:-2000]  # 古いものから捨てる
        for queue in list(self.subscribers):
            if queue.qsize() < 500:
                queue.put_nowait(entry)

    def seed_history(self) -> None:
        """起動時に「過去6時間分」のログを用意する。"""
        now = time.time()
        moments = sorted(now - random.uniform(60, 6 * 3600) for _ in range(140))
        self.log(f"Started {SERVICE_NAME}.service - Persona Chat in Discord bot.", source="systemd", at=moments[0] - 5)
        for message in STARTUP_LOGS:
            self.log(message, at=moments[0] - 4)
        for index, moment in enumerate(moments):
            if index % 9 == 4:
                self.log_conversation(at=moment, structured=index % 18 == 4)
            else:
                self.log(random_log_message(), at=moment)

    def log_conversation(self, at: float | None = None, *, structured: bool = False) -> None:
        """会話1件ぶんのログを、ボットと同じ形で複数行に分けて出す。"""
        label, question, attachments, answer = random.choice(CONVERSATIONS)
        total_in, total_out = random.randint(800, 6000), random.randint(80, 900)
        lines = [
            SEPARATOR_LINE,
            f"User ({label})：{question} / {attachments}",
            f"Prompt text count：{random.randint(1200, 9000)}",
            f"Prompt token count：{total_in}",
            f"Response token count：{total_out}",
            f"Total token count：{total_in + total_out}",
            f"AI：{answer[0]}",
            *answer[1:],
            SEPARATOR_LINE,
        ]
        for line in lines:
            message = logging_line(line, logger="persona_bot.utils.usage_graph", at=at) if structured else line
            self.log(message, at=at)

    async def live_loop(self, interval: tuple[float, float]) -> None:
        while True:
            await asyncio.sleep(random.uniform(*interval))
            if not self.active:
                continue
            roll = random.random()
            if roll < 0.04:
                for line in TRACEBACK_LOGS:
                    self.log(line, priority=3)
            elif roll < 0.16:
                self.log_conversation()
            else:
                self.log(random_log_message())

    # ---------------------------------------------------------- systemctl

    def service_show(self) -> str:
        now = time.monotonic()
        if self.active:
            self.memory_bytes = min(260e6, max(140e6, self.memory_bytes + random.uniform(-3e6, 3.4e6)))
            values = {
                "ActiveState": "active",
                "SubState": "running",
                "MainPID": str(self.pid),
                "MemoryCurrent": str(int(self.memory_bytes)),
                "ExecMainStartTimestampMonotonic": str(int((self.started_mono - self.boot_mono) * 1_000_000)),
                "CPUUsageNSec": str(int((self.cpu_seconds_base + (now - self.started_mono) * 0.031) * 1e9)),
            }
        elif self.failed:
            values = {
                "ActiveState": "failed",
                "SubState": "failed",
                "MainPID": "0",
                "MemoryCurrent": "[not set]",
                "ExecMainStartTimestampMonotonic": "0",
                "CPUUsageNSec": "[not set]",
            }
        else:
            values = {
                "ActiveState": "inactive",
                "SubState": "dead",
                "MainPID": "0",
                "MemoryCurrent": "[not set]",
                "ExecMainStartTimestampMonotonic": "0",
                "CPUUsageNSec": "[not set]",
            }
        values.update(
            {
                "Description": "Persona Chat in Discord bot",
                "LoadState": "loaded",
                "Result": "success",
                "UnitFileState": "enabled",
                "NRestarts": str(self.restarts),
            }
        )
        return "".join(f"{key}={value}\n" for key, value in values.items())

    async def control(self, action: str) -> None:
        async with self._busy:
            if action in ("stop", "restart") and self.active:
                self.log(f"Stopping {SERVICE_NAME}.service - Persona Chat in Discord bot...", source="systemd")
                await asyncio.sleep(0.8 * self.delay_scale)
                self.active = False
                self.stopped_at = time.time()
                self.log(f"{SERVICE_NAME}.service: Deactivated successfully.", source="systemd")
                self.log(f"Stopped {SERVICE_NAME}.service - Persona Chat in Discord bot.", source="systemd")
            if action in ("start", "restart") and not self.active:
                await asyncio.sleep(0.5 * self.delay_scale)
                self.active = True
                self.failed = False
                self.pid += random.randint(30, 400)
                self.started_mono = time.monotonic()
                self.cpu_seconds_base = 0.0
                self.memory_bytes = 150e6
                self.stopped_at = None
                self.gemini_calls = 0
                self.gemini_tokens = 0
                self.log(f"Started {SERVICE_NAME}.service - Persona Chat in Discord bot.", source="systemd")
                asyncio.get_running_loop().create_task(self._startup_logs())

    async def _startup_logs(self) -> None:
        broken = self.repo.head in self.repo.broken
        for message in STARTUP_LOGS:
            await asyncio.sleep(random.uniform(0.3, 0.9) * self.delay_scale)
            if not self.active:
                return
            if broken and message.startswith("ログイン成功"):
                # 起動に失敗する版: ログインの前に例外で落ち、systemd 上は failed になる
                for line in TRACEBACK_LOGS:
                    self.log(line, priority=3)
                self.active = False
                self.failed = True
                self.log(f"{SERVICE_NAME}.service: Main process exited, code=exited, status=1/FAILURE", source="systemd")
                return
            self.log(logging_line(message))

    # ---------------------------------------------------------- その他のコマンド

    def resources(self) -> str:
        uptime = time.monotonic() - self.boot_mono
        load1 = max(0.02, random.gauss(0.22 if self.active else 0.05, 0.08))
        total = 1_003_024_384
        self.host_memory_used = min(900e6, max(420e6, self.host_memory_used + random.uniform(-8e6, 8e6)))
        used = int(self.host_memory_used if self.active else self.host_memory_used - self.memory_bytes)
        available = total - used
        cache = 240_000_000
        free = max(0, available - cache)
        return (
            f"{uptime:.2f} {uptime * 1.7:.2f}\n"
            f"{load1:.2f} {load1 * 0.8:.2f} {load1 * 0.6:.2f} 2/187 {self.pid}\n"
            "2\n"
            "               total        used        free      shared  buff/cache   available\n"
            f"Mem:      {total} {used} {free} 1200000 {cache} {available}\n"
            "Swap:     2147479552 312000000 1835479552\n"
            " 47641534464 9831251968 37793505280\n"
        )


def random_log_message() -> str:
    weights = [weight for weight, _ in LIVE_LOG_TEMPLATES]
    template = random.choices([text for _, text in LIVE_LOG_TEMPLATES], weights=weights)[0]
    return template.format(
        model=random.choice(MODELS),
        sec=random.uniform(0.6, 4.2),
        tin=random.randint(800, 6000),
        tout=random.randint(80, 900),
        n=random.randint(1, 12),
    )


SERVER_INFO_OUTPUT = "fake-ssh-server\nLinux 6.8.0-1015-oracle\nUbuntu 24.04.1 LTS（模擬）\n"


def insights_output(bot: FakeBot) -> str:
    """remote_scripts/insights.py の出力の真似（Discord API・DB・ログから集めた状態）。"""
    now = time.time()

    def settings(persona, model, mode, grounding, auto_topic, interval, autonomous, saved=True,
                 source="saved", missing=None, builtin=False):
        return {
            "persona": persona, "persona_source": source, "persona_missing": missing, "persona_builtin": builtin,
            "model": model, "respond_mode": mode, "grounding": grounding,
            "auto_topic_enabled": auto_topic, "auto_topic_interval_hours": interval,
            "autonomous_enabled": autonomous, "saved": saved,
            "autonomous_stage_channel_id": None, "autonomous_voice_channel_id": None,
        }

    return json.dumps({
        "errors": [],
        "source_dir": "src",
        "version": "ver0.1.7(BETA)",
        "autonomous_supported": True,
        "persona_estimated": True,
        "discord": {
            "ok": True,
            "error": None,
            "api_rtt_ms": random.randint(60, 140),
            "bot_user": "PersonaBot#0000",
            "guilds": [
                {"name": "Persona Lab", "members": 42, "online": 11, "voice_channel": "雑談ステージ" if bot.active else None,
                 "settings": settings("ツンデレ", "Gemini 3.1 Flash Lite", "メンションのみに反応", True, True, 6, True)},
                {"name": "雑談ラウンジ", "members": 71, "online": 19, "voice_channel": None,
                 "settings": settings("お姉さん", "Gemini 3.5 Flash", "すべての発言に反応", False, True, 3, True,
                                      source="missing", missing="prompt_oneesan_old")},
                {"name": "テスト用サーバー", "members": 3, "online": 1, "voice_channel": None,
                 "settings": settings("標準（組み込み）", "Gemini 2.5 Flash", "メンションのみに反応", True, False, 6, False,
                                      saved=False, source="default", builtin=True)},
            ],
        },
        "dm_settings": settings("メイド", "Gemini 3.1 Flash Lite", "すべての発言に反応", False, False, 6, False),
        "activity": {
            "window_hours": 24,
            "gemini_calls": 214, "gemini_tokens": 512_340, "conversations": 188,
            "last_conversation_at": now - 140,
            "gateway_connects": 1, "gateway_resumes": 14, "gateway_invalidated": 0, "heartbeat_blocked": 0,
            "exceptions": 2,
            "autonomous_events": [
                {"at": now - 7400, "text": "配信を終了しました"},
                {"at": now - 740, "text": "配信を開始しました"},
            ],
        },
        "generated_at": now,
    }, ensure_ascii=False)


# ---------------------------------------------------------------- 架空の bot.db
#
# DB を読むスクリプト（集計・テーブルの中身）は、出力を作り物にせず、ここで作った架空の bot.db に対して
# 本当に実行する（件数と中身が食い違わず、スクリプトそのものも SSH 越しに確かめられるように）。
# 人・サーバー・チャンネル・メッセージの ID と名前、会話の文はすべて架空のもの。

# ボットの bot.db（スキーマ バージョン15）と同じ形。src/persona_bot/core/db_migrations.py の初版の定義に
# 移行をすべて適用した後の形を書き写したもの（ボットのコードは import しない）。
FAKE_DB_SCHEMA_VERSION = 15
FAKE_DB_TABLES = (
    "CREATE TABLE affinity (user_id TEXT PRIMARY KEY, affinity INTEGER NOT NULL DEFAULT 50, "
    "valence INTEGER NOT NULL DEFAULT 50, arousal INTEGER NOT NULL DEFAULT 50, last_interaction REAL NOT NULL DEFAULT 0)",
    "CREATE TABLE user_stats (user_id TEXT PRIMARY KEY, user_name TEXT NOT NULL DEFAULT '', "
    "total_count INTEGER NOT NULL DEFAULT 0, hourly TEXT NOT NULL DEFAULT '[]', first_seen TEXT NOT NULL DEFAULT '', "
    "last_seen TEXT NOT NULL DEFAULT '', recent_samples TEXT NOT NULL DEFAULT '[]', analysis TEXT, "
    "stats_opt_out INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE queue_tasks (task_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending', "
    "channel_id INTEGER NOT NULL DEFAULT 0, message_id INTEGER NOT NULL DEFAULT 0, author_id INTEGER NOT NULL DEFAULT 0, "
    "question TEXT NOT NULL DEFAULT '', attachment_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT '', "
    "updated_at TEXT NOT NULL DEFAULT '', finished_at TEXT, error TEXT)",
    "CREATE TABLE pending_log (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
    "question TEXT NOT NULL, response TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '')",
    "CREATE TABLE legacy_imports (source_name TEXT NOT NULL, content_hash TEXT NOT NULL, "
    "PRIMARY KEY (source_name, content_hash))",
    "CREATE TABLE profile (user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (user_id, key))",
    "CREATE TABLE guild_settings (guild_id INTEGER PRIMARY KEY, prompt_file TEXT NOT NULL DEFAULT '', "
    "model_name TEXT NOT NULL DEFAULT '', respond_mode TEXT NOT NULL DEFAULT '', grounding_enabled INTEGER NOT NULL DEFAULT 1, "
    "auto_topic_enabled INTEGER NOT NULL DEFAULT 0, auto_topic_interval_hours INTEGER NOT NULL DEFAULT 6, "
    "autonomous_enabled INTEGER NOT NULL DEFAULT 0, autonomous_stage_channel_id INTEGER NOT NULL DEFAULT 0, "
    "autonomous_voice_channel_id INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE anniversary_posts (guild_id INTEGER NOT NULL, user_id TEXT NOT NULL, label TEXT NOT NULL, "
    "posted_date TEXT NOT NULL, PRIMARY KEY (guild_id, user_id, label))",
    "CREATE TABLE reminders (reminder_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, "
    "user_name TEXT NOT NULL DEFAULT '', guild_id INTEGER NOT NULL DEFAULT 0, channel_id INTEGER NOT NULL, "
    "message TEXT NOT NULL, remind_at TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
    "recurrence TEXT NOT NULL DEFAULT 'none', recurrence_anchor TEXT NOT NULL DEFAULT '')",
    "CREATE TABLE feedback (message_id INTEGER NOT NULL, user_id TEXT NOT NULL, guild_id INTEGER NOT NULL DEFAULT 0, "
    "channel_id INTEGER NOT NULL DEFAULT 0, target_user_id TEXT NOT NULL DEFAULT '', model_name TEXT NOT NULL DEFAULT '', "
    "prompt_file TEXT NOT NULL DEFAULT '', rating INTEGER NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (message_id, user_id))",
    "CREATE INDEX idx_feedback_created_message ON feedback (created_at DESC, message_id DESC)",
    "CREATE INDEX idx_pending_log_user_id ON pending_log (user_id, id)",
    "CREATE INDEX idx_profile_key_user ON profile (key, user_id)",
)
# 架空の利用者（ID, 表示名）。最後の人は会話統計の収集を止めている
FAKE_USERS = [
    ("100000000000000101", "さくら"),
    ("100000000000000102", "たろう"),
    ("100000000000000103", "Mika"),
    ("100000000000000104", "ハルト"),
    ("100000000000000105", "ゆず"),
    ("100000000000000106", "Ren"),
]
FAKE_OPTED_OUT_USER = FAKE_USERS[-1][0]
FAKE_GUILD_IDS = (100000000000000001, 100000000000000002)
FAKE_CHANNEL_IDS = (100000000000001001, 100000000000001002, 100000000000001003)
# 17 桁を超えるメッセージ ID（JavaScript の数値では下の桁が丸まる大きさ）
FAKE_MESSAGE_ID_BASE = 1_300_000_000_000_000_000
FAKE_SAMPLES = [
    "おはよう！今日は何する？", "この前のゲームの続きやった？", "週末は雨らしいね", "おすすめの本ある？",
    "レポートの締め切りが近くて大変", "猫の写真を送るね", "カレーの作り方を教えて", "明日の天気は？",
    "最近ハマってる曲があるんだ", "ありがとう、助かった！",
]


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def make_fake_database(directory: Path) -> tuple[Path, Path]:
    """架空の bot.db と blacklist.json を作り直して、それぞれの場所を返す。中身は毎回同じ（乱数の種を固定）。"""
    rng = random.Random(103)
    now = datetime.now(timezone.utc)
    db_path = directory / "data" / "memory" / "bot.db"
    blacklist_path = directory / "data" / "ng" / "blacklist.json"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    blacklist_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)

    con = sqlite3.connect(db_path)
    try:
        for statement in FAKE_DB_TABLES:
            con.execute(statement)
        con.execute(f"PRAGMA user_version = {FAKE_DB_SCHEMA_VERSION}")
        con.execute(
            "INSERT INTO legacy_imports (source_name, content_hash) VALUES (?, ?)",
            ("pending_log.txt", "a" * 64),
        )
        for index, (user_id, name) in enumerate(FAKE_USERS):
            last = now - timedelta(hours=index * 7 + 1)
            con.execute(
                "INSERT INTO affinity VALUES (?, ?, ?, ?, ?)",
                (user_id, 82 - index * 9, 40 + rng.randint(0, 50), 30 + rng.randint(0, 60), last.timestamp()),
            )
            hourly = [rng.randint(0, 3) + (6 if 20 <= hour <= 23 else 0) for hour in range(24)]
            samples = rng.sample(FAKE_SAMPLES, 6)
            analysis = {
                "topics": [{"label": "ゲーム", "weight": 0.6}, {"label": "勉強", "weight": 0.3}, {"label": "料理", "weight": 0.1}],
                "big_five": {"openness": 72, "conscientiousness": 55, "extraversion": 64, "agreeableness": 80, "neuroticism": 35},
                "personality": "好奇心が強く、人当たりのよい話し方をします。",
                "computed_at": _iso(last),
                "sample_count": len(samples),
            }
            con.execute(
                "INSERT INTO user_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id, name, sum(hourly), json.dumps(hourly), _iso(now - timedelta(days=60 + index)), _iso(last),
                    json.dumps(samples, ensure_ascii=False), json.dumps(analysis, ensure_ascii=False),
                    1 if user_id == FAKE_OPTED_OUT_USER else 0,
                ),
            )
        user_ids = [user_id for user_id, _ in FAKE_USERS]

        for index, (status, error) in enumerate(
            [("completed", None), ("completed", None), ("failed", "PDF を読み取れませんでした（暗号化されています）。"),
             ("completed", None), ("processing", None), ("pending", None)]
        ):
            created = now - timedelta(hours=40 - index * 7)
            finished = _iso(created + timedelta(seconds=25)) if status in ("completed", "failed") else None
            con.execute(
                "INSERT INTO queue_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"task-{index + 1:04d}", status, FAKE_CHANNEL_IDS[index % 3], FAKE_MESSAGE_ID_BASE + 500 + index,
                    int(user_ids[index % 4]), "この資料の要点をまとめて", 1 + index % 2, _iso(created),
                    finished or _iso(created), finished, error,
                ),
            )

        for index in range(4):
            con.execute(
                "INSERT INTO pending_log (created_at, question, response, user_id) VALUES (?, ?, ?, ?)",
                (
                    (now - timedelta(minutes=50 - index * 12)).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    FAKE_SAMPLES[index], "いいね！その話、もっと聞かせて。\n次はどうするつもり？", user_ids[index % 2],
                ),
            )

        profile_rows = [
            (user_ids[0], "好きなもの", ["猫", "カレー", "RPG"]),
            (user_ids[0], "anniversaries", [{"label": "誕生日", "month": 5, "day": 1}]),
            (user_ids[0], "呼び方", "さくらちゃん"),
            (user_ids[1], "趣味", ["ドライブ", "音楽鑑賞"]),
            (user_ids[1], "anniversaries", [{"label": "誕生日", "month": 11, "day": 3}]),
            (user_ids[2], "所属", {"学校": "架空大学", "学年": 2}),
            (user_ids[3], "苦手なもの", ["早起き"]),
        ]
        for user_id, key, value in profile_rows:
            con.execute("INSERT INTO profile VALUES (?, ?, ?)", (user_id, key, json.dumps(value, ensure_ascii=False)))

        con.execute(
            "INSERT INTO guild_settings VALUES (0, '../prompts/prompt_default.txt', 'gemini-3.1-flash-lite', 'all', 0, 0, 6, 0, 0, 0)"
        )
        con.execute(
            "INSERT INTO guild_settings VALUES (?, '../prompts/prompt_tsun.txt', 'gemini-3-flash', 'mention', 1, 1, 3, 1, 0, 0)",
            (FAKE_GUILD_IDS[0],),
        )
        con.execute(
            "INSERT INTO guild_settings VALUES (?, '../prompts/prompt_default.txt', 'gemini-3.1-flash-lite', 'mention', 1, 0, 12, 0, 0, 0)",
            (FAKE_GUILD_IDS[1],),
        )

        con.execute(
            "INSERT INTO anniversary_posts VALUES (?, ?, '誕生日', ?)",
            (FAKE_GUILD_IDS[0], user_ids[1], (now - timedelta(days=15)).strftime("%Y-%m-%d")),
        )
        con.execute(
            "INSERT INTO anniversary_posts VALUES (?, ?, '誕生日', ?)",
            (FAKE_GUILD_IDS[0], user_ids[0], (now - timedelta(days=170)).strftime("%Y-%m-%d")),
        )

        reminder_rows = [
            ("レポートを提出する", timedelta(days=-3), "done", "none"),
            ("ゴミ出し", timedelta(hours=10), "pending", "weekly"),
            ("薬を飲む", timedelta(hours=2), "pending", "daily"),
            ("家賃の振り込み", timedelta(days=12), "pending", "monthly"),
            ("映画の予約", timedelta(days=-1), "canceled", "none"),
            ("友だちに電話する", timedelta(days=-6), "done", "none"),
            ("美容院の予約", timedelta(days=4), "pending", "none"),
        ]
        for index, (message, offset, status, recurrence) in enumerate(reminder_rows):
            user_id, name = FAKE_USERS[index % 3]
            remind_at = now + offset
            con.execute(
                "INSERT INTO reminders (user_id, user_name, guild_id, channel_id, message, remind_at, created_at, status, "
                "recurrence, recurrence_anchor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id, name, FAKE_GUILD_IDS[0] if index != 2 else 0, FAKE_CHANNEL_IDS[index % 3], message,
                    _iso(remind_at), _iso(remind_at - timedelta(days=2)), status, recurrence,
                    _iso(remind_at) if recurrence != "none" else "",
                ),
            )

        # 評価はページ送りを確かめられるよう、1ページ（50件）より多く作る
        models = ("gemini-3.1-flash-lite", "gemini-3-flash")
        personas = ("../prompts/prompt_tsun.txt", "../prompts/prompt_default.txt", "../prompts/prompt_oneesan.txt")
        for index in range(130):
            created = now - timedelta(minutes=index * 83 + rng.randint(0, 40))
            con.execute(
                "INSERT INTO feedback VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    FAKE_MESSAGE_ID_BASE + index * 7919, user_ids[index % 5], FAKE_GUILD_IDS[index % 2],
                    FAKE_CHANNEL_IDS[index % 3], user_ids[(index + 1) % 5], models[index % 2], personas[index % 3],
                    -1 if rng.random() < 0.18 else 1, _iso(created),
                ),
            )
        con.commit()
    finally:
        con.close()

    blacklist = {
        user_ids[3]: {"count": 1, "blocked": False},
        user_ids[4]: {"count": 3, "blocked": True},
    }
    blacklist_path.write_text(json.dumps(blacklist, ensure_ascii=False), encoding="utf-8")
    return db_path, blacklist_path


def run_db_script(bot: FakeBot, command: str, script: str) -> tuple[str, str, int]:
    """DB を読むスクリプトを、架空の bot.db に対して本当に実行する（サーバー上の場所だけを差し替える）。"""
    if bot.db_path is None or bot.blacklist_path is None:
        return "", "fake-ssh-server: 架空の bot.db がありません。\n", 1
    arguments = []
    for argument in shlex.split(command)[2:]:  # 「python3 -」の後ろ
        if argument.endswith("/data/memory/bot.db"):
            argument = str(bot.db_path)
        elif argument.endswith("/data/ng/blacklist.json"):
            argument = str(bot.blacklist_path)
        arguments.append(argument)
    completed = subprocess.run(
        [sys.executable, "-I", "-", *arguments],
        input=script, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return completed.stdout, completed.stderr, completed.returncode


# ---------------------------------------------------------------- SSH の受け口


async def handle_process(process: asyncssh.SSHServerProcess, bot: FakeBot) -> None:
    command = (process.command or "").strip()
    try:
        exit_code = await dispatch(command, process, bot)
    except (BrokenPipeError, ConnectionError, asyncssh.Error):
        return  # 相手が先に切った
    try:
        process.exit(exit_code)
    except (BrokenPipeError, ConnectionError, asyncssh.Error):
        pass


async def dispatch(command: str, process: asyncssh.SSHServerProcess, bot: FakeBot) -> int:
    if not command:
        process.stderr.write("fake-ssh-server: 対話シェルには対応していません。\n")
        return 1
    if command == "true":
        return 0
    if command == "date +%s":
        process.stdout.write(f"{int(time.time())}\n")
        return 0
    if match := re.fullmatch(r"cd ('[^']*'|\S+) && (.*)", command):
        answer = bot.repo.respond(match.group(2))
        if answer is not None:
            stdout, stderr, code = answer
            process.stdout.write(stdout)
            process.stderr.write(stderr)
            return code
    if command == f"systemctl show {SERVICE_NAME} --no-pager --all --property=Job":
        process.stdout.write("Job=\n")
        return 0
    if command.startswith(f"systemctl show {SERVICE_NAME} "):
        process.stdout.write(bot.service_show())
        return 0
    if match := re.fullmatch(r"sudo -n systemctl (start|stop|restart) (\S+)", command):
        if match.group(2) != SERVICE_NAME:
            process.stderr.write(f"Failed to {match.group(1)} {match.group(2)}.service: Unit not found.\n")
            return 5
        await bot.control(match.group(1))
        return 0
    if match := re.search(r"journalctl -u \S+ --since @(\d+) ", command):
        since_us = int(match.group(1)) * 1_000_000
        for entry in bot.entries:
            if int(entry["__REALTIME_TIMESTAMP"]) >= since_us:
                process.stdout.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return 0
    if "journalctl" in command and f"-u {SERVICE_NAME} " in command:
        count_match = re.search(r"-n (\d+)", command)
        count = int(count_match.group(1)) if count_match else 10
        for entry in bot.entries[-count:]:
            process.stdout.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if command.endswith(" -f"):
            await follow_logs(process, bot)
        return 0
    if "/proc/loadavg" in command:
        process.stdout.write(bot.resources())
        return 0
    if command.startswith("hostname"):
        process.stdout.write(SERVER_INFO_OUTPUT)
        return 0
    if command.startswith("python3 - --insights "):
        await process.stdin.read()  # 集計スクリプトを最後まで受け取る（中身は実行しない）
        process.stdout.write(insights_output(bot) + "\n")
        return 0
    if command.startswith("python3 - "):
        # DB の集計・テーブルの中身: 受け取ったスクリプトを、架空の bot.db に対して本当に実行する
        script = await process.stdin.read()
        stdout, stderr, code = await asyncio.to_thread(run_db_script, bot, command, script)
        process.stdout.write(stdout)
        process.stderr.write(stderr)
        return code
    process.stderr.write(f"fake-ssh-server: 未対応のコマンドです: {command}\n")
    return 127


async def follow_logs(process: asyncssh.SSHServerProcess, bot: FakeBot) -> None:
    """journalctl -f の真似。クライアントがチャンネルを閉じるまで新しいログを送り続ける。"""
    queue: asyncio.Queue = asyncio.Queue()
    bot.subscribers.add(queue)
    # クライアントがチャンネルを閉じると、標準入力の読み込みが終わる
    closed = asyncio.ensure_future(process.stdin.read())
    closed.add_done_callback(lambda future: future.cancelled() or future.exception())
    try:
        while True:
            getter = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({getter, closed}, return_when=asyncio.FIRST_COMPLETED)
            if closed in done:
                getter.cancel()
                return
            process.stdout.write(json.dumps(getter.result(), ensure_ascii=False) + "\n")
    finally:
        bot.subscribers.discard(queue)
        closed.cancel()


class _FakeServer(asyncssh.SSHServer):
    def __init__(self, bot: FakeBot) -> None:
        self._bot = bot
        self._conn: asyncssh.SSHServerConnection | None = None

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self._conn = conn
        self._bot.connections.add(conn)  # サーバー側から切るときのために覚えておく

    def connection_lost(self, exc: Exception | None) -> None:
        self._bot.connections.discard(self._conn)

    def begin_auth(self, username: str) -> bool:
        return True  # 認証を必須にする（鍵認証のみ）

    def password_auth_supported(self) -> bool:
        return False


def ensure_keys(key_dir: Path) -> None:
    key_dir.mkdir(parents=True, exist_ok=True)
    host_key = key_dir / "host_key"
    client_key = key_dir / "client_key"
    if not host_key.exists():
        asyncssh.generate_private_key("ssh-ed25519", comment="fake-ssh-server").write_private_key(host_key)
    if not client_key.exists():
        key = asyncssh.generate_private_key("ssh-ed25519", comment="persona-bot-console")
        key.write_private_key(client_key)
        key.write_public_key(key_dir / "client_key.pub")


def write_known_hosts(key_dir: Path, host: str, port: int) -> None:
    """ダッシュボードがこのサーバーのホスト鍵を検証するための known_hosts を作る。"""
    public_key = asyncssh.read_private_key(key_dir / "host_key").export_public_key().decode().split()
    key_text = " ".join(public_key[:2])  # 「鍵の種類 本体」だけ（コメントは不要）
    names = sorted({host, "127.0.0.1", "localhost"})
    lines = [f"[{name}]:{port} {key_text}" if port != 22 else f"{name} {key_text}" for name in names]
    (key_dir / "known_hosts").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def start_fake_server(
    host: str,
    port: int,
    key_dir: Path,
    *,
    live_interval: tuple[float, float] = (2.0, 6.0),
    delay_scale: float = 1.0,
) -> tuple[asyncssh.SSHAcceptor, FakeBot, int, asyncio.Task]:
    """模擬サーバーを起動して (受け口, 模擬ボット, 実際のポート, ログ生成タスク) を返す。port=0 なら空きポートを使う。"""
    ensure_keys(key_dir)
    bot = FakeBot(delay_scale=delay_scale)
    # DB の作成（書き込みと同期）でイベントループを止めないよう、別スレッドで作る
    bot.db_path, bot.blacklist_path = await asyncio.to_thread(make_fake_database, key_dir / "bot")
    bot.seed_history()
    acceptor = await asyncssh.create_server(
        lambda: _FakeServer(bot),
        host,
        port,
        server_host_keys=[str(key_dir / "host_key")],
        authorized_client_keys=str(key_dir / "client_key.pub"),
        process_factory=lambda process: handle_process(process, bot),
    )
    actual_port = acceptor.sockets[0].getsockname()[1]
    write_known_hosts(key_dir, host, actual_port)
    live_task = asyncio.get_running_loop().create_task(bot.live_loop(live_interval))
    return acceptor, bot, actual_port, live_task
