// 画面で使う小さなアイコン。飾りなので、読み上げからは隠す（aria-hidden）。

type IconProps = { size?: number };

function svgProps(size: number) {
  return {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    "aria-hidden": true,
    focusable: false,
  } as const;
}

const stroke = {
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round",
  strokeLinejoin: "round",
} as const;

export function LogoMark({ size = 28 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden focusable={false}>
      <rect width="64" height="64" rx="18" fill="var(--accent)" />
      <path
        d="M18 20h28a6 6 0 0 1 6 6v14a6 6 0 0 1-6 6H33l-9 8v-8h-6a6 6 0 0 1-6-6V26a6 6 0 0 1 6-6z"
        fill="var(--on-accent)"
      />
      <circle cx="25" cy="33" r="3" fill="var(--accent)" />
      <circle cx="39" cy="33" r="3" fill="var(--warm)" />
    </svg>
  );
}

export function GitHubIcon({ size = 18 }: IconProps) {
  return (
    <svg {...svgProps(size)}>
      <path
        fill="currentColor"
        d="M12 2a10 10 0 0 0-3.16 19.49c.5.09.68-.22.68-.48v-1.7c-2.78.6-3.37-1.34-3.37-1.34-.46-1.16-1.11-1.47-1.11-1.47-.91-.62.07-.61.07-.61 1 .07 1.53 1.03 1.53 1.03.9 1.52 2.34 1.08 2.91.83.09-.65.35-1.09.63-1.34-2.22-.25-4.56-1.11-4.56-4.94 0-1.09.39-1.98 1.03-2.68-.1-.25-.45-1.27.1-2.65 0 0 .84-.27 2.75 1.02a9.56 9.56 0 0 1 5 0c1.91-1.29 2.75-1.02 2.75-1.02.55 1.38.2 2.4.1 2.65.64.7 1.03 1.59 1.03 2.68 0 3.84-2.34 4.68-4.57 4.93.36.31.68.92.68 1.85v2.75c0 .27.18.58.69.48A10 10 0 0 0 12 2z"
      />
    </svg>
  );
}

export function SunIcon({ size = 18 }: IconProps) {
  return (
    <svg {...svgProps(size)} {...stroke}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  );
}

export function MoonIcon({ size = 18 }: IconProps) {
  return (
    <svg {...svgProps(size)} {...stroke}>
      <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" />
    </svg>
  );
}

export function PlayIcon({ size = 14 }: IconProps) {
  return (
    <svg {...svgProps(size)}>
      <path fill="currentColor" d="M7 4.5v15a1 1 0 0 0 1.5.86l12.5-7.5a1 1 0 0 0 0-1.72L8.5 3.64A1 1 0 0 0 7 4.5z" />
    </svg>
  );
}

export function PauseIcon({ size = 14 }: IconProps) {
  return (
    <svg {...svgProps(size)}>
      <path fill="currentColor" d="M6 4h4v16H6zM14 4h4v16h-4z" />
    </svg>
  );
}

export function ArrowIcon({ size = 16 }: IconProps) {
  return (
    <svg {...svgProps(size)} {...stroke}>
      <path d="M5 12h14M13 6l6 6-6 6" />
    </svg>
  );
}

export function HashIcon({ size = 18 }: IconProps) {
  return (
    <svg {...svgProps(size)} {...stroke}>
      <path d="M9 4 7 20M17 4l-2 16M4 9h16M3 15h16" />
    </svg>
  );
}

export function SpeakerIcon({ size = 18 }: IconProps) {
  return (
    <svg {...svgProps(size)} {...stroke}>
      <path d="M4 10v4h3l5 4V6L7 10H4z" />
      <path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11" />
    </svg>
  );
}

export function StageIcon({ size = 18 }: IconProps) {
  return (
    <svg {...svgProps(size)} {...stroke}>
      <circle cx="12" cy="10" r="2.5" />
      <path d="M7.5 14.5a6.5 6.5 0 1 1 9 0M5 18a10 10 0 1 1 14 0M12 15v6" />
    </svg>
  );
}

export function ThreadIcon({ size = 18 }: IconProps) {
  return (
    <svg {...svgProps(size)} {...stroke}>
      <path d="M5 5h14v9H11l-4 4v-4H5z" />
      <path d="M9 9h6" />
    </svg>
  );
}

export function PaperclipIcon({ size = 14 }: IconProps) {
  return (
    <svg {...svgProps(size)} {...stroke}>
      <path d="M20 11.5 12 19.5a5 5 0 0 1-7-7l8-8a3.5 3.5 0 0 1 5 5l-8 8a2 2 0 0 1-3-3l7.5-7.5" />
    </svg>
  );
}
