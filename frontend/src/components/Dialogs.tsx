import { useEffect, useRef, type ReactNode } from 'react';
import { X } from 'lucide-react';
export function Modal(p: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  className?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null),
    close = useRef(p.onClose);
  close.current = p.onClose;
  useEffect(() => {
    const active = document.activeElement as HTMLElement | null;
    const dialog = ref.current!;
    dialog.showModal();
    const cancel = (event: Event) => {
      event.preventDefault();
      close.current();
    };
    dialog.addEventListener('cancel', cancel);
    return () => {
      dialog.removeEventListener('cancel', cancel);
      dialog.close();
      active?.focus();
    };
  }, []);
  return (
    <dialog
      ref={ref}
      className={`v2-dialog ${p.className || ''}`}
      aria-label={p.title}
      onKeyDown={(event) => {
        if (event.key !== 'Tab') return;
        const fields = [
          ...event.currentTarget.querySelectorAll<HTMLElement>(
            'button, input, select, textarea, a[href], [tabindex]',
          ),
        ].filter(
          (element) =>
            !element.hasAttribute('disabled') &&
            element.tabIndex >= 0 &&
            element.getClientRects().length,
        );
        const first = fields[0],
          last = fields[fields.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }}
    >
      <header>
        <h2>{p.title}</h2>
        <button aria-label="Закрыть диалог" onClick={p.onClose}>
          <X size={20} />
        </button>
      </header>
      {p.children}
    </dialog>
  );
}
