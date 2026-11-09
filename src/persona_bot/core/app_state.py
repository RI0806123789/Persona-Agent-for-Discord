
import logging
import asyncio
from weakref import WeakValueDictionary
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from persona_bot.config.choices import (
    AUTO_TOPIC_INTERVAL_CHOICES,
    MODEL_CHOICES,
    RESPOND_MODE_CHOICES,
)
from persona_bot.config.constants import (
    DM_SETTINGS_GUILD_KEY,
)
from persona_bot.config.personas import current_persona_catalog, persona_file_name
from persona_bot.core.background import run_in_thread, wait_for_cleanup
from persona_bot.core.guild_settings_fields import GUILD_SETTINGS_DEFAULTS, GUILD_SETTINGS_FIELDS


logger = logging.getLogger(__name__)


@dataclass
class AutonomousSession:
    """進行中の自律行動（ボイス/ステージでの「配信」）1件分の実行時状態。

    ボットが自分からボイス/ステージチャンネルへ入っている間だけ存在し、
    終了時に破棄される。ボイス接続そのものが再起動をまたげないため、
    この状態も **インメモリのみ**（永続化しない）。

    実際の接続は `guild.voice_client` が唯一の情報源なので、ここには
    VoiceClient を保持しない（保持すると、外部から切断されたときに
    実態と食い違った参照が残る）。
    """

    # 配信しているボイス/ステージチャンネル。コメントを受け付けるのもこのチャンネル
    # 付属のチャットだけ（他のテキストチャンネルからの発言はコメントとして扱わない）。
    channel_id: int
    is_stage: bool
    started_at: datetime
    # 最後に人間のコメントが届いた時刻。届かないまま
    # AUTONOMOUS_IDLE_STOP_MINUTES が過ぎたら配信を自動終了する。
    last_human_at: datetime
    # 最後にボットが喋った時刻（独り言の間隔を測るための基準）。
    last_spoke_at: datetime
    # ステージインスタンス（配信枠）を自分で開始したか。
    # 他の人が開いていたステージに相乗りした場合は False にし、終了時に閉じない。
    stage_started_by_bot: bool = False
    # 生成・発話中フラグ。見回りは1分ごとに来るため、前回の生成が終わる前に
    # 次の独り言が走らないようにする（同じチャンネルへの二重投稿を防ぐ）。
    busy: bool = False
    # 最後にコメントへ応答した時刻。コメントの拾いすぎを抑えるクールダウンの基準。
    # **last_spoke_at とは別に持つこと** — last_spoke_at は独り言でも更新されるため、
    # そちらを流用すると「独り言を聞いて反応した最初のコメント」を捨ててしまう
    # （いちばん拾いたいコメントなので、独り言の直後は必ず応答できるようにする）。
    last_comment_reply_at: datetime | None = None
    # これまでに喋った独り言の回数（プロンプトで「話し始めか続きか」を伝えるのに使う）。
    turn_count: int = 0
    # 自分で開いた枠のID。同じチャンネルで他の枠が開き直されても誤って閉じない。
    stage_instance_id: int | None = None


@dataclass(frozen=True)
class ResponseSettings:
    """1つの応答の生成・評価・読み上げで共用する、変更されない設定。"""

    prompt_file: str
    model_name: str
    grounding_enabled: bool


@dataclass
class GuildSettings:
    """1つの Discord サーバー（ギルド）に紐づく可変設定。

    ペルソナ・モデル・応答モード・グラウンディング・会話リセット地点・自発会話の
    設定はサーバーごとに独立させたいため、BotState 直下ではなくこちらに持たせる。
    DM（サーバーに属さない会話）はギルドが存在しないため、BotState.dm_settings に
    集約された「全DM共有の1スロット」として扱う（guild_id=None 相当）。
    """

    # 空文字は「既定のペルソナに従う」（まだ選んでいない）。実際に使うペルソナは
    # snapshot_response() が一覧から解決する（personas.py）。
    current_prompt_file: str = GUILD_SETTINGS_DEFAULTS["prompt_file"]
    current_model_name: str = GUILD_SETTINGS_DEFAULTS["model_name"]
    current_respond_mode: str = GUILD_SETTINGS_DEFAULTS["respond_mode"]
    grounding_enabled: bool = GUILD_SETTINGS_DEFAULTS["grounding_enabled"]
    # 会話リセット地点は「実行時に自然に作り直される一時状態」のため永続化しない。
    channel_reset_points: dict[int, int] = field(default_factory=dict)

    # --- 永続化のための紐づけ（BotState が attach() で設定する） ---
    # 自分がどのサーバーの設定かと、保存先の Database。DM は guild_id=None。
    # DB が未接続（None）のときは保存を試みない（DB を使わないテスト等で動くように）。
    guild_id: int | None = None
    _db: Any = field(default=None, repr=False, compare=False)

    # persist() の直列化用。並行した保存の書き込み順が入れ替わって、
    # 古い値で新しい値を上書きしないようにする（persist の説明を参照）。
    _persist_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    _active: bool = field(default=True, init=False, repr=False, compare=False)

    # --- 自発的な会話 ---
    # 機能の有効/無効と無会話しきい値（時間）。/functions → 💬 自発会話 で変更する。
    auto_topic_enabled: bool = GUILD_SETTINGS_DEFAULTS["auto_topic_enabled"]
    auto_topic_interval_hours: int = GUILD_SETTINGS_DEFAULTS["auto_topic_interval_hours"]
    # 以下2つは監視の実行時状態。会話が続けば自然に再構築されるため永続化しない。
    # 監視対象チャンネル: ボットが会話に参加したチャンネル ID → 最後に人間の発言があった時刻
    auto_topic_channels: dict[int, datetime] = field(default_factory=dict)
    # チャンネル ID → 最後に自動で話題提供した時刻（沈黙が続く限り連投しないための記録）
    auto_topic_posted_at: dict[int, datetime] = field(default_factory=dict)

    # --- 自律行動（ボイス/ステージへ自分から出て「配信」する） ---
    # 機能の有効/無効。自発会話（テキストへの話題提供）とは独立して切り替えられる。
    # 「話したいゲージ」が満タンになるまでの時間は auto_topic_interval_hours を共用する
    # （設定項目を増やさず /functions → 💬 自発会話 パネル1枚で完結させるため）。
    autonomous_enabled: bool = GUILD_SETTINGS_DEFAULTS["autonomous_enabled"]
    # 管理者が指定した配信先（⚙️ キャラ設定 → 🎙️ 配信先）。None は「指定なし（自動で選ぶ）」。
    # 使えない指定（削除済み・権限不足）は autonomous_stage.pick_channel が自動選択へ戻す。
    autonomous_stage_channel_id: int | None = GUILD_SETTINGS_DEFAULTS["autonomous_stage_channel_id"]
    autonomous_voice_channel_id: int | None = GUILD_SETTINGS_DEFAULTS["autonomous_voice_channel_id"]
    # 以下3つは実行時状態。会話が続けば自然に再構築されるため永続化しない。
    # 進行中の配信（無いときは None）。
    autonomous_session: AutonomousSession | None = None
    # 最後に人間が発言した時刻（このサーバーのどのチャンネルでもよい）。
    # 「話したいゲージ」の基礎値（無会話時間）を測る基準。None は「まだ観測していない」。
    autonomous_last_activity: datetime | None = None
    # 直近の人間の発言時刻（新しい順ではなく到着順）。ゲージの加点に使う。
    # AUTONOMOUS_ACTIVITY_WINDOW_MINUTES より古い分と、
    # AUTONOMOUS_ACTIVITY_SAMPLES_MAX を超えた分は追加時に捨てる。
    autonomous_recent_activity: list[datetime] = field(default_factory=list)
    # 配信の開始に失敗したときの再試行の抑制。失敗してもゲージは満タンのままなので、
    # これが無いと見回りのたび（＝毎分）に接続→失敗→切断を永久に繰り返す。
    # この時刻より前は開始を試みない。連続失敗回数に応じて待ち時間が延びる。
    autonomous_start_retry_at: datetime | None = None
    autonomous_start_failures: int = 0

    # --- 記念日・誕生日の自動お祝い ---
    # "{user_id}:{label}" → 最後にお祝いを投稿した日付（"YYYY-MM-DD"）。
    # 同じ記念日に同じサーバーで二重投稿しないための記録。DB にも保存され、
    # 起動時に復元される（当日に再起動しても同じお祝いを繰り返さないため）。
    anniversary_posted_dates: dict[str, str] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        """退出前の設定を保持したパネルから、古い値を再利用させない。"""
        return self._active

    def snapshot_response(self) -> ResponseSettings:
        """非同期の処理を始める前に、今回の応答に使う設定を確定する。"""
        return ResponseSettings(
            # 未選択・ファイルが見つからない間は既定のペルソナで応答する（設定値は消さない）
            prompt_file=current_persona_catalog().resolve(self.current_prompt_file),
            model_name=self.current_model_name,
            grounding_enabled=self.grounding_enabled,
        )

    def attach(self, db: Any, guild_id: int | None) -> None:
        """永続化先の Database と自分のギルドIDを紐づける（BotState が生成時に呼ぶ）。"""
        self._db = db
        self.guild_id = guild_id

    @property
    def persist_key(self) -> int:
        """DB 上でこの設定を表すギルドID。DM はギルドIDが無いため 0 を使う。"""
        return DM_SETTINGS_GUILD_KEY if self.guild_id is None else self.guild_id

    def apply_persisted(self, values: dict[str, Any]) -> None:
        """DB から読み出した設定値を反映する（起動時・再参加時の復元用）。

        値が空・欠落している項目は既定値のままにする（列を増やした直後の行など、
        中途半端なデータでボットが起動しなくなることを避けるため）。
        """
        self._apply_persisted_prompt_file(values.get("prompt_file"))
        for key, attribute, choices, default in (
            ("model_name", "current_model_name", MODEL_CHOICES, GUILD_SETTINGS_DEFAULTS["model_name"]),
            ("respond_mode", "current_respond_mode", RESPOND_MODE_CHOICES, GUILD_SETTINGS_DEFAULTS["respond_mode"]),
        ):
            value = values.get(key)
            if value is None or value == "":
                continue
            valid = isinstance(value, str) and any(choice.value == value for choice in choices)
            if valid:
                setattr(self, attribute, value)
            else:
                logger.warning(f'サーバー設定: 保存済みの {key} が無効なため既定値へ戻します。')
                setattr(self, attribute, default)
        for key, attribute, kind, _ in GUILD_SETTINGS_FIELDS:
            if kind == "bool" and values.get(key) is not None:
                setattr(self, attribute, bool(values[key]))
            elif kind == "channel" and key in values:
                setattr(self, attribute, _persisted_channel_id(key, values[key]))
        interval = values.get("auto_topic_interval_hours")
        if (isinstance(interval, int) and not isinstance(interval, bool)
                and any(choice.value == interval for choice in AUTO_TOPIC_INTERVAL_CHOICES)):
            self.auto_topic_interval_hours = interval
        elif interval is not None:
            logger.warning('サーバー設定: 保存済みの auto_topic_interval_hours が無効なため既定値へ戻します。')
            self.auto_topic_interval_hours = GUILD_SETTINGS_DEFAULTS["auto_topic_interval_hours"]

    def _apply_persisted_prompt_file(self, value: Any) -> None:
        """保存済みのペルソナを復元する。

        ペルソナの形の値なら、ファイルが見つからなくても値を残す。Git の更新で
        キャラ別のファイルが一時的に消えた環境でも、ファイルを戻せば元のキャラに
        戻れるようにするため（見つからない間は snapshot_response() が既定で応答する）。
        """
        if value is None or value == "":
            return
        file_name = persona_file_name(value)
        if file_name is None:
            logger.warning('サーバー設定: 保存済みの prompt_file が無効なため既定値へ戻します。')
            self.current_prompt_file = GUILD_SETTINGS_DEFAULTS["prompt_file"]
            return
        self.current_prompt_file = value
        if not current_persona_catalog().contains(value):
            logger.warning('サーバー設定: 保存済みのペルソナが prompts/ に見つかりません。ファイルを戻すまで既定のペルソナで応答します。')

    async def persist(self) -> None:
        """現在の設定値を DB へ保存する。設定を変更した UI 側が変更確定後に呼ぶ。

        保存に失敗してもチャットや UI 操作は止めない（フェイルオープン）。その場合、
        変更はインメモリには残るため、次回の再起動まではそのまま使える。

        保存は `_persist_lock` で直列化し、**保存する値はロックを取ってから読む**。
        保存は行の総入れ替えなので、2つの保存が別スレッドで並ぶと、先に呼ばれた
        古い値が後から書かれて新しい値を上書きしうる。ロックの中で読めば、後から
        書く側が常にその時点の最新の値を書く。`run_in_thread` はキャンセルされても
        書き込みの完了までロックを保つ（素の `asyncio.to_thread` だとキャンセルの
        瞬間にロックが外れ、書き込み中の古い値が次の保存を追い越して残りうる）。
        """
        if self._db is None:
            return
        async with self._persist_lock:
            # ロック待ち中に退出した保存も、新しい設定を上書きさせない。
            if not self.is_active:
                return
            try:
                await run_in_thread(
                    self._db.guild_settings_save,
                    self.persist_key,
                    **{name: getattr(self, attribute) for name, attribute, _, _ in GUILD_SETTINGS_FIELDS},
                )
            except Exception as error:
                logger.error(f'ギルド設定の保存に失敗しました (guild_id=<ID>): {type(error).__name__}', exc_info=error)

    async def record_anniversary_post(self, user_id: str, label: str, posted_date: str) -> None:
        """記念日のお祝いを投稿したことを、メモリと DB の両方へ記録する。

        再参加時の復元は同じサーバーの保存を待つ。ロック待ち中に見回りが
        キャンセルされても、投稿済みの記録を保存してからキャンセルを伝える。
        """
        self.anniversary_posted_dates[f"{user_id}:{label}"] = posted_date
        if self._db is None:
            return
        async def save() -> None:
            async with self._persist_lock:
                try:
                    await run_in_thread(
                        self._db.anniversary_post_record, self.persist_key, user_id, label, posted_date
                    )
                except Exception as error:
                    logger.error(f'記念日の投稿記録の保存に失敗しました (guild_id=<ID>): {type(error).__name__}', exc_info=error)

        await wait_for_cleanup(save(), label="記念日の投稿記録")


def _persisted_channel_id(key: str, value: Any) -> int | None:
    """保存済みの配信先のチャンネルIDを復元する。0・未設定は「指定なし」。

    bool や負の数など、チャンネルIDになりえない値は警告して「指定なし」に戻す
    （指定なしなら従来どおり自動で選ぶため、起動は止めない）。
    """
    if value is None or (type(value) is int and value == 0):
        return None
    if type(value) is int and value > 0:
        return value
    logger.warning(f'サーバー設定: 保存済みの {key} が無効なため「指定なし」へ戻します。')
    return None


@dataclass
class _GuildSettingsLoad:
    """同じサーバーの初回読込を直列化し、退出で古い結果を無効にする。"""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    valid: bool = True


@dataclass
class BotState:
    # ギルドID → そのサーバー専用の設定。初回は get_guild_async() がDBから復元する。
    guild_settings: dict[int, GuildSettings] = field(default_factory=dict)
    # DM（ギルドに属さない会話）専用の設定。すべての DM で共有する単一スロット。
    dm_settings: GuildSettings = field(default_factory=GuildSettings)

    last_activity_timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # ウェルカムメッセージを投稿済みのサーバーID。**インメモリのみ**（永続化しない）。
    # on_guild_join は実際に招待されたときにしか発火せず、再起動・再接続では
    # 発火しないため、プロセスをまたぐ重複投稿は原理的に起きない。この集合は
    # 「同一プロセス内で万一イベントが重複して届いた場合」の保険にすぎないので、
    # auto_topic_channels と同じ「実行時に自然に再構築される一時状態」として扱う。
    welcomed_guild_ids: set[int] = field(default_factory=set)
    # 退出処理中だけ保持する所有印。再参加で外し、古い後片付けを無効にする。
    guild_removals: dict[int, object] = field(default_factory=dict, repr=False, compare=False)
    memory_background_started: bool = False
    document_worker_started: bool = False
    commands_synced: bool = False
    auto_topic_background_started: bool = False
    autonomous_background_started: bool = False
    anniversary_background_started: bool = False
    reminder_background_started: bool = False
    feedback_prune_background_started: bool = False

    # 設定の保存先。restore_from_database() で紐づけられる。
    _db: Any = field(default=None, repr=False, compare=False)
    # 読込中・待機中だけ保持し、過去に退出したサーバーの識別子を溜めない。
    _guild_loads: WeakValueDictionary[int, _GuildSettingsLoad] = field(
        default_factory=WeakValueDictionary, repr=False, compare=False,
    )
    # 退出前の保存と再参加後の復元・保存も直列化する。参照がなくなれば回収される。
    _guild_persist_locks: WeakValueDictionary[int, asyncio.Lock] = field(
        default_factory=WeakValueDictionary, repr=False, compare=False,
    )

    def _guild_persist_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_persist_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_persist_locks[guild_id] = lock
        return lock

    def get_guild(self, guild_id: int | None) -> GuildSettings:
        """復元済みの設定を同期的に返す。DBを使わない場合は既定値で生成する。

        DM（guild_id が None）は全 DM 共有の dm_settings を返す。
        DB接続済みの未登録サーバーは get_guild_async() で取得する。同期の入口から
        DBを読まないことで、保存中の共有ロック待ちでもイベントループを止めない。
        """
        if guild_id is None:
            return self.dm_settings
        settings = self.guild_settings.get(guild_id)
        if settings is None:
            if self._db is not None:
                raise RuntimeError("未復元のサーバー設定は get_guild_async() で取得してください。")
            settings = GuildSettings(_persist_lock=self._guild_persist_lock(guild_id))
            settings.attach(self._db, guild_id)
            self.guild_settings[guild_id] = settings
        return settings

    def get_guild_if_ready(self, guild_id: int | None) -> GuildSettings | None:
        """同期の活動観測用。DB復元が必要なら見送り、DB未接続なら従来どおり生成する。"""
        if self._db is not None and guild_id is not None and guild_id not in self.guild_settings:
            return None
        return self.get_guild(guild_id)

    async def get_guild_async(self, guild_id: int | None) -> GuildSettings:
        """保存済みの設定と記念日を、イベントループを塞がずに初回復元する。

        同じサーバーへの並行要求は同じ設定を共有する。他サーバーとDMの取得は待たせない。
        読込失敗・キャンセル時は未復元の既定値を登録せず、次回アクセスで再試行する。
        """
        if guild_id is None or guild_id in self.guild_settings or self._db is None:
            return self.get_guild(guild_id)
        loading = self._guild_loads.get(guild_id)
        if loading is None or not loading.valid:
            # 退出で無効になった読込が残っていても掴まず、新しい読込に差し替える
            # （古い読込の待機者は valid の確認で中止される）。
            loading = _GuildSettingsLoad()
            self._guild_loads[guild_id] = loading
        async with loading.lock:
            if not loading.valid:
                raise RuntimeError("サーバー退出により設定の復元を中止しました。")
            settings = self.guild_settings.get(guild_id)
            if settings is None:
                persist_lock = self._guild_persist_lock(guild_id)
                async with persist_lock:
                    # 退出前に始まった保存だけを待つ。旧世代の読込は再参加を待たせない。
                    pass
                values, posted = await run_in_thread(self._db.guild_state_load, guild_id)
                if not loading.valid:
                    raise RuntimeError("サーバー退出により設定の復元を中止しました。")
                settings = GuildSettings(_persist_lock=persist_lock)
                settings.apply_persisted(values)
                settings.anniversary_posted_dates.update(posted)
                settings.attach(self._db, guild_id)
                self.guild_settings[guild_id] = settings
            return settings

    def forget_guild(self, guild_id: int) -> None:
        """退出したサーバーの一時設定と、待機中の古い読込結果を無効にする。"""
        settings = self.guild_settings.pop(guild_id, None)
        if settings is not None:
            settings._active = False
        loading = self._guild_loads.pop(guild_id, None)
        if loading is not None:
            loading.valid = False

    def restore_from_database(self, db: Any) -> None:
        """Database を紐づけ、保存済みの設定と記念日の投稿記録をメモリへ復元する。

        起動時に1回だけ呼ぶ（bot.py）。読み込みに失敗しても既定値で起動を続ける
        （設定が戻らないだけで、ボット自体は動かせるようにするため）。
        """
        self._db = db
        self.dm_settings.attach(db, None)
        # 復元より前に get_guild() で作られた設定にも DB をつなぐ。つながないと、
        # その設定は persist() しても DB へ書かれず、再起動で既定値に戻ってしまう。
        for guild_id, guild_settings in self.guild_settings.items():
            guild_settings.attach(db, guild_id)

        try:
            saved_settings, saved_posts = db.guild_state_load_all()
        except Exception as error:
            logger.error(f'保存済みのギルド設定の読み込みに失敗しました: {type(error).__name__}', exc_info=error)
            return

        for guild_key, values in saved_settings.items():
            self._settings_for_key(guild_key).apply_persisted(values)
        for guild_key, posted in saved_posts.items():
            self._settings_for_key(guild_key).anniversary_posted_dates.update(posted)

        if saved_settings:
            logger.info(f'データベース: {len(saved_settings)} 件のサーバー設定を復元しました。')

    def _settings_for_key(self, guild_key: int) -> GuildSettings:
        """起動時の一括復元先を返す。取得済みの値を使うため、ここではDBを読み直さない。"""
        if guild_key == DM_SETTINGS_GUILD_KEY:
            return self.dm_settings
        settings = self.guild_settings.get(guild_key)
        if settings is None:
            settings = GuildSettings(_persist_lock=self._guild_persist_lock(guild_key))
            settings.attach(self._db, guild_key)
            self.guild_settings[guild_key] = settings
        return settings

    def iter_all_guild_settings(self):
        """(guild_id, GuildSettings) を全件返す。DM は guild_id=None として含む。

        記念日チェックなど「全サーバー + DM を横断して見回る」処理に使う。
        """
        yield None, self.dm_settings
        yield from self.guild_settings.items()

    def touch(self) -> None:
        self.last_activity_timestamp = datetime.now(timezone.utc)
