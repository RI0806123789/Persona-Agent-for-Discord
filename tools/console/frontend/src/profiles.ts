import type { Profile } from "./types";

/**
 * 見本の値（YOUR_...）のまま書き換えられていない項目。
 * この項目を返さない古いバックエンド（起動したまま画面だけ新しくなった場合など）では、空として扱う。
 */
export function unsetFieldsOf(profile: Profile | null | undefined): string[] {
  return profile?.unset_fields ?? [];
}

/** 案内文に差し込む「YOUR_ で始まる値（項目名・項目名）」。 */
export function unsetFieldsText(fields: string[]): string {
  return `YOUR_ で始まる値（${fields.join("・")}）`;
}
