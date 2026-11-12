// 紹介サイトに出す文言の、唯一の置き場所。
//
// ★ ボットの機能を足したり既定値を変えたりしたら、README・ヘルプ（src/persona_bot/views/help_view.py）と
//   合わせて、このファイルの文言も直すこと（本文が実装と食い違わないことが最優先）。
// ★ 公開ページなので、個人情報・Discord の ID・API キー・サーバーの実パス・実際の会話や記憶・
//   キャラ別のプロンプトを書かないこと。疑似 Discord 画面の登場人物と会話はすべて架空。
// ★ ボットの招待 URL は載せない（Issue #410 の決定。自分でホストする前提の説明にとどめる）。
//
// このファイルは Node.js の標準テスト（tests/）からも読み込む。値の import を足さないこと
// （型だけの import は実行時に消えるのでよい）。

import type { DemoAuthor, DemoMessage } from "./demoTimeline";

export const SITE_NAME = "Persona Agent for Discord";

const REPOSITORY_URL = "https://github.com/RI0806123789/Persona-Agent-for-Discord";

/** 外部へのリンク。すべてこのリポジトリの中を指す */
export const LINKS = {
  repository: REPOSITORY_URL,
  setup: `${REPOSITORY_URL}#readme`,
  releases: `${REPOSITORY_URL}/releases`,
  contributing: `${REPOSITORY_URL}/blob/main/CONTRIBUTING.md`,
  codeOfConduct: `${REPOSITORY_URL}/blob/main/CODE_OF_CONDUCT.md`,
  security: `${REPOSITORY_URL}/blob/main/SECURITY.md`,
  license: `${REPOSITORY_URL}/blob/main/LICENSE.md`,
} as const;

export const NAV_ITEMS = [
  { href: "#features", label: "できること" },
  { href: "#story", label: "制作背景" },
  { href: "#start", label: "使い方" },
  { href: "#stack", label: "技術" },
] as const;

export const HERO = {
  eyebrow: "キャラクターと暮らす Discord ボット",
  titleLines: ["呼べば答える。", "呼ばなくても、", "話しかけてくる。"],
  lead:
    "Persona Agent for Discord は、あなたが作ったキャラクターになりきって会話するボットです。" +
    "文字でも声でも話せて、話したことを覚えていて、ときどき向こうから声をかけてきます。",
  primaryAction: { href: "#features", label: "できることを見る" },
  secondaryAction: { href: LINKS.repository, label: "GitHub で見る" },
  notes: ["無料で公開（MIT ライセンス）", "自分のパソコンやサーバーで動かす", "キャラクターは自分で作れる"],
} as const;

// ---------------------------------------------------------------------------
// 疑似 Discord 画面（動作イメージ）
// ---------------------------------------------------------------------------

export type DemoCastMember = {
  name: string;
  /** アイコンに出す 1 文字 */
  initial: string;
  /** 名前の横に出す札（ボットの「アプリ」など）。無ければ出さない */
  badge?: string;
};

/** 登場人物。全員が架空で、実在の利用者・キャラクターとは関係ない */
export const DEMO_CAST: Record<DemoAuthor, DemoCastMember> = {
  bot: { name: "こはく", initial: "こ", badge: "アプリ" },
  user: { name: "はる", initial: "は" },
  friend: { name: "ゆき", initial: "ゆ" },
  helper: { name: "調査担当", initial: "調", badge: "アプリ" },
};

export type DemoChannelKind = "text" | "voice" | "stage" | "thread";

export type DemoScenario = {
  id: string;
  /** タブに出す名前 */
  label: string;
  channelKind: DemoChannelKind;
  channelName: string;
  /** 画面の下に出す、この場面の説明 */
  caption: string;
  messages: readonly DemoMessage[];
};

export const DEMO_SCENARIOS: readonly DemoScenario[] = [
  {
    id: "chat",
    label: "会話",
    channelKind: "text",
    channelName: "雑談",
    caption: "名前を呼んで（メンションして）話しかけるだけ。画像や PDF を添えると、その中身も踏まえて答えます。",
    messages: [
      { author: "user", mention: true, text: "おはよう！今日は何して過ごそうかな" },
      {
        author: "bot",
        text: "おはよう、はる。よく眠れた？ 外は晴れてるし、散歩がてら朝ごはんを買いに行くのはどうかな。",
        reactions: ["👍", "👎"],
      },
      { author: "user", mention: true, text: "これも見て、空いてる日を教えて", attachment: "週末の予定.pdf" },
      { author: "bot", text: "見たよ。土曜の午後がまるごと空いてるね。気になってた映画、そこに入れちゃおうか。" },
    ],
  },
  {
    id: "memory",
    label: "記憶",
    channelKind: "text",
    channelName: "雑談",
    caption: "話したことを覚えていて、次の会話に活かします。記憶は人ごとに分かれていて、ほかの人には混ざりません。",
    messages: [
      { author: "user", mention: true, text: "チーズケーキには目がないんだよね" },
      { author: "bot", text: "ふふ、覚えておくね。はるはチーズケーキが大好き、っと。" },
      { author: "user", mention: true, text: "なんだか甘いものが食べたい気分", dividerBefore: "数日後" },
      { author: "bot", text: "それなら駅前のチーズケーキはどう？ 目がないって言ってたでしょ。" },
    ],
  },
  {
    id: "reminder",
    label: "リマインダー",
    channelKind: "text",
    channelName: "雑談",
    caption: "会話の中で頼むだけで、その時刻に知らせます。毎日・毎週・毎月の繰り返しも頼めます。",
    messages: [
      { author: "user", mention: true, text: "明日の朝9時に「燃えるゴミの日」って教えて" },
      { author: "bot", text: "わかった。明日の 9:00 に「燃えるゴミの日」をお知らせするね。" },
      { author: "bot", text: "⏰ @はる リマインダーです: 燃えるゴミの日", dividerBefore: "翌日 9:00" },
    ],
  },
  {
    id: "voice",
    label: "音声会話",
    channelKind: "voice",
    channelName: "通話ルーム",
    caption: "ボイスチャンネルに呼ぶと、声で会話できます。何人かで話しても順番に聞き取り、全員が同意するまで始めません。",
    messages: [
      { author: "user", spoken: true, text: "こはく、聞こえる？" },
      { author: "bot", spoken: true, text: "聞こえてるよ。今日はどんな一日だった？" },
      { author: "friend", spoken: true, text: "わたしも混ざっていい？" },
      { author: "bot", spoken: true, text: "もちろん。ゆきの話も聞かせて。" },
    ],
  },
  {
    id: "autonomous",
    label: "自律配信",
    channelKind: "stage",
    channelName: "会話相手募集中",
    caption: "しばらく誰も話していないと、ボットのほうからステージやボイスに出てきて、ひとりで話しはじめます。",
    messages: [
      { author: "bot", spoken: true, text: "……誰もいないみたいだから、ひとりで話してるね。最近、夜が涼しくなってきたなあ。" },
      { author: "friend", text: "お、何してるの？" },
      { author: "bot", spoken: true, text: "あ、ゆきだ。来てくれたんだ。ちょうど話し相手がほしかったところ。" },
    ],
  },
  {
    id: "subagent",
    label: "サブエージェント",
    channelKind: "thread",
    channelName: "初心者向けの観葉植物を調べる",
    caption: "「サブエージェントを使って」と頼むと、調べ物を別の AI に任せ、結果をキャラクターの言葉で伝えます。",
    messages: [
      { author: "user", mention: true, text: "サブエージェントを使って、初心者向けの観葉植物を調べて" },
      { author: "bot", text: "任せて。調査担当にお願いしてくるね。" },
      {
        author: "helper",
        text: "調査が終わりました。育てやすさ・日当たり・水やりの回数で 3 種類に絞りました。",
        links: ["参考リンク 1", "参考リンク 2"],
      },
      {
        author: "bot",
        text: "お待たせ。初めてならポトスがいちばん育てやすいみたい。日陰にも強いんだって。",
        reactions: ["👍", "👎"],
      },
    ],
  },
];

export const DEMO_NOTE = "動作のイメージです。登場人物と会話はすべて架空で、実際の画面とは細部が異なります。";

// ---------------------------------------------------------------------------
// 機能紹介
// ---------------------------------------------------------------------------

export type Feature = { icon: string; title: string; body: string; note?: string };
export type FeatureGroup = { id: string; title: string; lead: string; features: readonly Feature[] };

export const FEATURES_INTRO = {
  eyebrow: "できること",
  title: "話す、覚える、自分から動く。",
  lead: "ふだんの雑談から調べ物まで。キャラクターのまま、いろいろなことを引き受けます。",
} as const;

export const FEATURE_GROUPS: readonly FeatureGroup[] = [
  {
    id: "talk",
    title: "話す",
    lead: "文字でも、声でも。",
    features: [
      {
        icon: "💬",
        title: "名前を呼ぶだけで会話",
        body: "メンションして話しかけると、キャラクターの口調で返事が返ってきます。設定を変えれば、すべての発言に反応させることもできます。",
      },
      {
        icon: "🎙️",
        title: "声で会話",
        body: "ボイスチャンネルに呼ぶと、声でやりとりできます。何人かで話しても順番に聞き取り、話の途中で割り込むこともできます。",
        note: "参加者全員の同意がそろうまで、声の聞き取りは始まりません。",
      },
      {
        icon: "🖼️",
        title: "画像や文書も読める",
        body: "画像や PDF・テキストのファイルを添えると、その中身を踏まえて答えます。",
      },
      {
        icon: "🔍",
        title: "調べてから答える",
        body: "最新の話題は Google 検索で調べ、参考にしたページのリンクを添えて答えます。検索は切ることもできます。",
      },
    ],
  },
  {
    id: "remember",
    title: "覚える",
    lead: "一緒に過ごした時間が、積み重なる。",
    features: [
      {
        icon: "🧠",
        title: "話したことを覚えている",
        body: "好きなものや続けていることを覚え、次の会話に活かします。",
        note: "記憶は人ごとに完全に分かれていて、ほかの人の記憶と混ざりません。",
      },
      {
        icon: "⏰",
        title: "リマインダー",
        body: "「明日の 9 時に教えて」と会話で頼むだけ。その時刻に、頼んだチャンネルで知らせます。毎日・毎週・毎月の繰り返しも頼めます。",
      },
      {
        icon: "🎂",
        title: "記念日のお祝い",
        body: "会話の中で伝えた誕生日や記念日を覚えておき、当日にボットのほうからお祝いします。",
      },
      {
        icon: "📋",
        title: "会話のまとめ",
        body: "チャンネルの会話を、誰が何を言ったかが分かる議事録にまとめます。期間は 1 時間から 1 か月まで選べます。",
      },
    ],
  },
  {
    id: "initiative",
    title: "自分から動く",
    lead: "呼ばれるのを、待っているだけじゃない。",
    features: [
      {
        icon: "💭",
        title: "話題を振ってくる",
        body: "会話が途絶えたチャンネルに、それまでの話の流れを踏まえた話題を投げかけます。",
        note: "はじめは OFF です。使うときだけ ON にします。",
      },
      {
        icon: "📡",
        title: "ひとりで配信をはじめる",
        body: "しばらく誰も話していないと、自分からボイスやステージに出てきて独り言を続け、コメントには配信者のように応えます。",
        note: "はじめは OFF です。深夜の時間帯には出てきません。",
      },
      {
        icon: "🧭",
        title: "調べ物を任せる",
        body: "「サブエージェントを使って」と頼むと、調べ物を別の AI に任せます。結果はキャラクターの言葉で伝え、作業の経緯はスレッドに残ります。",
      },
    ],
  },
  {
    id: "safe",
    title: "安心して置ける",
    lead: "みんなが居る場所だから。",
    features: [
      {
        icon: "🛡️",
        title: "不適切な発言を見張る",
        body: "NG ワードと AI の判定で不適切な発言を検知し、3 回でその人への応答を止めます。ボット自身の返事も、送る前に確かめます。",
      },
      {
        icon: "🔒",
        title: "自分のデータは自分で決める",
        body: "会話の統計は本人にしか見えません。記録を止めることも、溜まったデータを消すことも、自分でできます。",
      },
      {
        icon: "🎭",
        title: "キャラクターは自分で作る",
        body: "性格や口調を書いたファイルを置くだけで、自分だけのキャラクターになります。声の高さや速さもキャラクターごとに変えられます。",
        note: "設定はサーバーごとに別々で、ほかのサーバーに影響しません。",
      },
    ],
  },
];

// ---------------------------------------------------------------------------
// 制作背景・目的
// ---------------------------------------------------------------------------

// ★ 仮の文（Issue #410）。ボットの機能から言える範囲で書いた下書きで、制作者本人の経緯ではない。
//   公開する前に、制作者が自分の言葉で書き直すこと。
export const STORY = {
  eyebrow: "制作背景",
  title: "「道具」ではなく、「相手」になるボットを。",
  paragraphs: [
    "Discord のボットの多くは、呼ばれたときにだけ答える便利な道具です。用が済めば、また黙って待っています。",
    "でも、いつものサーバーに居てほしいのは、こちらが話したことを覚えていて、静かな夜にはふと声をかけてくれるような「相手」ではないか。そんな考えから、このボットを作りはじめました。",
  ],
} as const;

// ★ 仮の文（Issue #410）。上の STORY と同じく、公開する前に制作者が見直すこと。
export const PURPOSE = {
  eyebrow: "目的",
  title: "めざしていること",
  items: [
    {
      title: "キャラクターと自然に話せる",
      body: "コマンドを覚えなくても、名前を呼んで話しかけるだけ。文字でも声でも、同じキャラクターとして応えます。",
    },
    {
      title: "一緒に過ごした時間が残る",
      body: "好きなもの、記念日、頼んだ予定。話したことが積み重なって、その人との会話になっていきます。",
    },
    {
      title: "安心してサーバーに置ける",
      body: "記憶は人ごとに分け、不適切な発言は見張り、声を聞くときは全員の同意を待ちます。",
    },
  ],
} as const;

// ---------------------------------------------------------------------------
// 使い方
// ---------------------------------------------------------------------------

export const START = {
  eyebrow: "使い方",
  title: "はじめるまでの 3 ステップ",
  lead: "このボットは、公開の招待リンクを配っていません。ソースコードを受け取り、自分のボットとして動かして使います。",
  steps: [
    {
      title: "自分の環境で動かす",
      body: "Python と、Google Gemini・Discord の 2 つのキーを用意して起動します。手順は README に順番に書いてあります。",
      action: { href: LINKS.setup, label: "セットアップの手順を読む" },
    },
    {
      title: "名前を呼んで話しかける",
      body: "ボットをサーバーに入れたら、メンションして話しかけるだけ。特別なコマンドは要りません。",
    },
    {
      title: "機能パネルを開く",
      body: "/functions と入力すると、自分にだけ見えるパネルが開きます。キャラクターの切り替えや、まとめ・統計・リマインダーの確認はここから。",
    },
  ],
} as const;

// ---------------------------------------------------------------------------
// 技術スタック・使用しているツール
// ---------------------------------------------------------------------------

export type StackItem = { name: string; role: string };

export const STACK = {
  eyebrow: "技術スタック",
  title: "ボットを動かしているもの",
  lead: "中身が気になる方へ。追加のソフトをなるべく入れずに、小さなサーバーでも動くように作っています。",
  items: [
    { name: "Python 3.11 以上", role: "ボット本体の言語" },
    { name: "discord.py", role: "Discord とのやりとり（文字・ボイス）" },
    { name: "Google Gemini", role: "会話の生成・内容の判定・文書の要約" },
    { name: "Gemini Live", role: "ボイスチャンネルでの声の会話" },
    { name: "SQLite", role: "記憶・設定・リマインダーの保存" },
    { name: "edge-tts / gTTS", role: "配信での読み上げ" },
    { name: "miniaudio / NumPy", role: "音声の変換と加工" },
    { name: "Matplotlib", role: "統計や利用量のグラフ" },
  ] satisfies readonly StackItem[],
} as const;

export const TOOLS = {
  eyebrow: "使用しているツール",
  title: "開発をささえる道具",
  lead: "コードを書くところから、公開の前の確認まで。",
  items: [
    { name: "Claude Code", role: "ソースコードとプロンプトの見本の生成" },
    { name: "Codex", role: "実装とレビューの分担" },
    { name: "GitHub Actions", role: "Windows・Linux での回帰テストと、このページの公開" },
    { name: "Dependabot / pip-audit", role: "ライブラリの更新の提案と、脆弱性の検査" },
    { name: "React / TypeScript / Vite", role: "このページと、開発者用の管理画面" },
    { name: "draw.io", role: "しくみを説明するフロー図" },
  ] satisfies readonly StackItem[],
} as const;

// ---------------------------------------------------------------------------
// 更新履歴への入口・免責事項・フッター
// ---------------------------------------------------------------------------

export const UPDATES = {
  eyebrow: "更新履歴",
  title: "少しずつ、育てています。",
  lead: "新しい機能や直したところは、リリースごとにまとめています。",
  actions: [
    { href: LINKS.releases, label: "リリース一覧を見る" },
    { href: LINKS.contributing, label: "開発に参加する" },
  ],
} as const;

export const DISCLAIMER = {
  title: "フィクションに関する免責事項",
  body:
    "ペルソナはすべて架空のキャラクターです。性格・口調はエンターテインメント目的のロールプレイ指示であり、" +
    "実在の人物・団体・属性を表現・差別・攻撃する意図は一切ありません。" +
    "ボットの出力は AI による創作で、開発者の見解を反映するものではありません。",
  voiceNote:
    "声での会話では、同意した参加者の声が Google の Gemini Live へ送られます。" +
    "利用の前に、Google と Discord の利用規約（年齢の条件を含む）を確認してください。",
} as const;

export const FOOTER = {
  links: [
    { href: LINKS.repository, label: "GitHub" },
    { href: LINKS.setup, label: "セットアップ" },
    { href: LINKS.releases, label: "更新履歴" },
    { href: LINKS.contributing, label: "貢献ガイド" },
    { href: LINKS.codeOfConduct, label: "行動規範" },
    { href: LINKS.security, label: "セキュリティポリシー" },
  ],
  license: { href: LINKS.license, label: "MIT License" },
  notes: [
    "ソースコードは Claude Code によって生成されています。",
    "このページはアクセスの計測を行っていません。",
  ],
} as const;
