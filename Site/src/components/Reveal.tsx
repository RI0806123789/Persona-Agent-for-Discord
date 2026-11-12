import type { CSSProperties, ReactNode } from "react";
import { useInView } from "../hooks";

type RevealProps = {
  children: ReactNode;
  className?: string;
  /** 並んだ要素を少しずつ遅らせて出すときの順番（0 始まり） */
  order?: number;
};

/** 画面に入ったときに、下からふわっと現れる入れ物（動きを減らす設定では CSS が動きを止める） */
export function Reveal({ children, className, order = 0 }: RevealProps) {
  const [ref, inView] = useInView<HTMLDivElement>({ once: true });
  const style = { "--reveal-order": order } as CSSProperties;
  const classes = ["reveal", inView ? "is-visible" : "", className ?? ""].filter(Boolean).join(" ");
  return (
    <div ref={ref} className={classes} style={style}>
      {children}
    </div>
  );
}
