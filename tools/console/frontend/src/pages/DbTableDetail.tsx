// データページで選んだ bot.db のテーブルの中身。50 行ずつ取り寄せ、行を開くと全部の列を見せる。
// 会話・記憶・リマインダーの本文と Discord の ID も届くので、伏せ字モードでは列の種類ごとに隠す
// （dbTables.ts の isSecretKind）。統計の収集を止めた人の発言サンプルと解析は、サーバーから持ち帰らない。
import { Fragment, useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { api, errorMessage, errorSafeMessage } from "../api";
import { CollapseBody } from "../components/Collapse";
import { WavyProgress } from "../components/expressive";
import { Button, Card, Chip, EmptyState, IconButton, InlineError, Notice } from "../components/ui";
import { DB_TABLE_SPECS, namesInPage, resolveColumns, type ColumnSpec } from "../dbTables";
import { formatNumber } from "../format";
import { usePresence } from "../hooks";
import { redactError, usePrivacy } from "../privacy";
import type { DbTablePage } from "../types";
import { CellValue, RowFields } from "./DbCells";

// テストと呼び出し側が従来どおり DbTableDetail から取れるよう、行の全列の描画も再公開する（実体は DbCells.tsx）。
export { RowFields };

const PAGE_SIZE = 50;

export function DbTableDetail({
  table,
  refreshToken,
  onClose,
  onNames,
}: {
  table: string;
  refreshToken: number;
  onClose: () => void;
  /** 取り寄せた行に出てきた利用者の表示名（伏せ字モードで、ほかの画面の文からも隠すため） */
  onNames: (names: string[]) => void;
}) {
  const spec = DB_TABLE_SPECS[table];
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<DbTablePage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errorSafe, setErrorSafe] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set());
  const privacy = usePrivacy();
  const requestId = useRef(0);

  const load = useCallback(
    async (nextOffset: number) => {
      const current = ++requestId.current;
      setLoading(true);
      try {
        const page = await api.dbTable(table, nextOffset, PAGE_SIZE);
        if (current !== requestId.current) return;
        setData(page);
        setError(null);
        setErrorSafe(null);
        setExpanded(new Set());
        onNames(namesInPage(page));
      } catch (caught) {
        if (current !== requestId.current) return;
        setError(errorMessage(caught));
        setErrorSafe(errorSafeMessage(caught));
      } finally {
        if (current === requestId.current) setLoading(false);
      }
    },
    [table, onNames],
  );

  useEffect(() => {
    void load(offset);
    return () => {
      requestId.current += 1;
    };
  }, [load, offset, refreshToken]);

  const columns = data ? resolveColumns(table, data.columns) : [];
  const listColumns = columns.filter((column) => column.list || column.kind === "raw");
  const rows = data?.rows ?? [];
  const total = data?.total ?? 0;
  // 表示する範囲。行が0件のページ（取得の間に総行数が減ったなど）は 0–0 とし、「0–50 / 30 行」のように逆転させない。
  const from = rows.length ? (data?.offset ?? 0) + 1 : 0;
  const to = rows.length ? (data?.offset ?? 0) + rows.length : 0;
  // ページ送りの判定は表示とは別にする。0件のページでも、いまの位置から先へは進ませない（#478）。
  const reachedEnd = (data?.offset ?? 0) + rows.length >= total;

  const toggle = (index: number) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });

  return (
    <Card
      id="db-table-detail"
      title={`${table} の中身`}
      icon="table"
      source="ssh"
      className="db-detail"
      actions={
        <>
          <Button variant="text" size="xs" icon="refresh" onClick={() => void load(offset)} busy={loading}>
            読み直す
          </Button>
          <IconButton icon="close" label="中身を閉じる" size="xs" onClick={onClose} />
        </>
      }
    >
      <div className="db-detail-head">
        <span className="title-small">{spec?.label ?? table}</span>
        {spec && <span className="body-medium muted">{spec.description}</span>}
      </div>

      <div className="db-detail-toolbar">
        {spec && <Chip icon="arrowDown">{spec.order}</Chip>}
        <span className="body-medium mono db-range" aria-live="polite">
          {data ? `${formatNumber(from)}–${formatNumber(to)} / ${formatNumber(total)} 行` : "—"}
        </span>
        <span className="db-pager">
          <IconButton
            icon="chevronLeft"
            label={`前の ${PAGE_SIZE} 行`}
            disabled={loading || !data || data.offset === 0}
            onClick={() => setOffset(Math.max(0, (data?.offset ?? 0) - PAGE_SIZE))}
          />
          <IconButton
            icon="chevronRight"
            label={`次の ${PAGE_SIZE} 行`}
            disabled={loading || !data || reachedEnd}
            onClick={() => setOffset((data?.offset ?? 0) + PAGE_SIZE)}
          />
        </span>
      </div>

      {privacy.enabled ? (
        <Notice icon="eyeOff">伏せ字モード: 本文・名前・ID を隠しています（日時・数値・状態・モデル・ペルソナは表示します）。</Notice>
      ) : (
        <Notice icon="shield">読み取り専用（mode=ro）で、この 50 行だけを取り寄せています。スクリーンショットを共有するときは伏せ字モード（h）にしてください。</Notice>
      )}
      {error && <InlineError>{redactError(error, errorSafe, privacy)}</InlineError>}
      {data?.errors.map((message) => <InlineError key={message}>{message}</InlineError>)}
      {!data && !error && <WavyProgress label="取り寄せています" />}
      {data && rows.length === 0 && !data.errors.length && <EmptyState icon="table" title="行がありません" />}

      {data && rows.length > 0 && (
        <div className={`table-wrap db-table-wrap ${loading ? "is-loading" : ""}`}>
          <table className="m3-table is-compact db-table">
            <caption className="visually-hidden">
              {table} の {from}〜{to} 行目（全 {total} 行）
            </caption>
            <thead>
              <tr>
                <th className="db-expand-cell">
                  <span className="visually-hidden">開く</span>
                </th>
                {listColumns.map((column) => (
                  <th key={column.key} className={isNumeric(column) ? "num" : ""} title={column.key}>
                    {column.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => {
                const open = expanded.has(index);
                const detailId = `db-row-${index}`;
                return (
                  <Fragment key={`${data.offset}:${index}`}>
                    <tr
                      className={`db-row ${open ? "is-open" : ""}`}
                      // 1行ずつ少し遅らせて浮かび上がらせる（styles.css の row-in）
                      style={{ "--row-index": index } as CSSProperties}
                      onClick={() => {
                        // 文字を選んでコピーしようとしているときは開閉しない
                        if (!window.getSelection()?.toString()) toggle(index);
                      }}
                    >
                      <td className="db-expand-cell">
                        <IconButton
                          icon="expandMore"
                          size="xs"
                          label={open ? "この行を閉じる" : "この行の全部の列を見る"}
                          aria-expanded={open}
                          aria-controls={open ? detailId : undefined}
                          className="db-expand"
                          onClick={(event) => {
                            event.stopPropagation();
                            toggle(index);
                          }}
                        />
                      </td>
                      {listColumns.map((column) => (
                        <td key={column.key} className={`db-cell kind-${column.kind} ${isNumeric(column) ? "num" : ""}`}>
                          <CellValue column={column} row={row} page={data} />
                        </td>
                      ))}
                    </tr>
                    <DetailRow open={open} id={detailId} colSpan={listColumns.length + 1}>
                      <RowFields columns={columns} row={row} page={data} />
                    </DetailRow>
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function isNumeric(column: ColumnSpec): boolean {
  return column.kind === "count" || column.kind === "hours";
}

/** 開いた行の下に足す行。縮み終わるまで残し、高さを伸び縮みさせて開閉する。 */
function DetailRow({ open, id, colSpan, children }: { open: boolean; id: string; colSpan: number; children: ReactNode }) {
  const { present, onExited } = usePresence(open);
  if (!present) return null;
  return (
    <tr className="db-row-detail" id={id}>
      <td colSpan={colSpan}>
        <CollapseBody open={open} onExited={onExited}>
          <div className="db-row-detail-body">{children}</div>
        </CollapseBody>
      </td>
    </tr>
  );
}
