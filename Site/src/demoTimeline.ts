// 疑似 Discord 画面の「どの発言をいつ出すか」を決める、表示に依存しない計算。
// React もブラウザも使わないので、Node.js の標準テストからそのまま読み込める
// （このファイルから値を import しないこと。型だけの import は実行時に消えるのでよい）。

/** 発言者の種類。名前と見た目は content.ts の DEMO_CAST が決める */
export type DemoAuthor = "user" | "friend" | "bot" | "helper";

/** 疑似 Discord 画面に出す 1 件の発言 */
export type DemoMessage = {
  author: DemoAuthor;
  text: string;
  /** 先頭にボット宛てのメンションを付ける */
  mention?: boolean;
  /** 添付ファイルの名前（チップで表示する） */
  attachment?: string;
  /** 発言の下に付くリアクション */
  reactions?: readonly string[];
  /** 参考リンクのチップ（Google 検索の出典のイメージ） */
  links?: readonly string[];
  /** ボイスチャンネルで声として話した発言（吹き出しに音声の印を付ける） */
  spoken?: boolean;
  /** この発言の前に入れる区切り（「数日後」など、時間が飛んだことを示す） */
  dividerBefore?: string;
};

/** 1 件の発言を出す時刻（再生開始からのミリ秒） */
export type TimelineStep = {
  /** 入力中の表示を出し始める時刻。人間の発言は入力中を出さないので showAt と同じ */
  typingAt: number;
  /** 発言を出す時刻 */
  showAt: number;
};

export type Timeline = {
  steps: readonly TimelineStep[];
  /** 最後の発言を読み終えるまでの時刻。ここを過ぎたら次の場面へ進む */
  totalMs: number;
};

export type TimelineFrame = {
  /** 表示済みの発言の件数 */
  visible: number;
  /** 入力中を表示する発言の番号。無ければ null */
  typingIndex: number | null;
  /** 場面を最後まで再生し終えたか */
  finished: boolean;
};

// 動きの速さ。読みやすさを優先して、実際のチャットよりゆっくりにしている
const FIRST_DELAY_MS = 500;
const GAP_MS = 1100;
const TYPING_MS_PER_CHAR = 32;
const TYPING_MIN_MS = 800;
const TYPING_MAX_MS = 1800;
const HOLD_BASE_MS = 2600;
const HOLD_MS_PER_CHAR = 28;
const HOLD_MAX_MS = 5200;

/** 入力中を出すのは、返事を「考えている」ように見せたいボット側の発言だけ */
function typingDuration(message: DemoMessage): number {
  if (message.author !== "bot" && message.author !== "helper") return 0;
  const byLength = message.text.length * TYPING_MS_PER_CHAR;
  return Math.min(TYPING_MAX_MS, Math.max(TYPING_MIN_MS, byLength));
}

/** 発言の一覧から、それぞれを出す時刻を決める */
export function buildTimeline(messages: readonly DemoMessage[]): Timeline {
  const steps: TimelineStep[] = [];
  let cursor = FIRST_DELAY_MS;
  for (const message of messages) {
    const typingAt = cursor;
    const showAt = typingAt + typingDuration(message);
    steps.push({ typingAt, showAt });
    cursor = showAt + GAP_MS;
  }
  const last = messages[messages.length - 1];
  const lastShown = steps.length > 0 ? steps[steps.length - 1].showAt : 0;
  // 最後の発言は、長さに応じて読む時間を取ってから次の場面へ進む
  const hold = last ? Math.min(HOLD_MAX_MS, HOLD_BASE_MS + last.text.length * HOLD_MS_PER_CHAR) : 0;
  return { steps, totalMs: lastShown + hold };
}

/** 再生開始から elapsedMs たった時点の表示内容を返す */
export function frameAt(timeline: Timeline, elapsedMs: number): TimelineFrame {
  let visible = 0;
  let typingIndex: number | null = null;
  for (let index = 0; index < timeline.steps.length; index += 1) {
    const step = timeline.steps[index];
    if (elapsedMs >= step.showAt) {
      visible = index + 1;
    } else {
      // 次に出る発言だけが入力中になりうる（その先の発言はまだ始まっていない）
      if (elapsedMs >= step.typingAt && step.typingAt < step.showAt) typingIndex = index;
      break;
    }
  }
  return { visible, typingIndex, finished: elapsedMs >= timeline.totalMs };
}

/** 動きを減らす設定のとき用。全部の発言を最初から出した状態 */
export function finishedFrame(timeline: Timeline): TimelineFrame {
  return { visible: timeline.steps.length, typingIndex: null, finished: true };
}
