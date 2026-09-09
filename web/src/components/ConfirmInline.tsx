import { Icon } from "./Icon";

type ConfirmInlineProps = {
  /** The destructive verb, e.g. "删除" / "清除" / "移出". Renders as
   *  「icon · 删除？· 确认删除 · 取消」. */
  verb: string;
  pending: boolean;
  onConfirm: () => void;
  onCancel: () => void;
};

/** The two-step destructive confirm, one component for every call site
 *  (previously four hand-rolled 确认/取消 pairs with drifted wording).
 *  Replaces the trigger in place — no modal, no toast, per the project
 *  spec — and wraps the armed state in a danger-tinted cluster so it is
 *  unmistakable which button is now live.
 */
export function ConfirmInline({ verb, pending, onConfirm, onCancel }: ConfirmInlineProps) {
  return (
    <span className="confirm-inline">
      <Icon name="alert-triangle" size={13} />
      <span className="confirm-verb">{verb}？</span>
      <button type="button" data-variant="danger" onClick={onConfirm} disabled={pending}>
        {pending ? `${verb}中…` : `确认${verb}`}
      </button>
      <button type="button" onClick={onCancel} disabled={pending}>
        取消
      </button>
    </span>
  );
}
