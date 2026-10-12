import { useEffect, useRef, type ReactNode } from 'react';

export function Modal({ labelledBy, onClose, children }: { labelledBy: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const dialog = ref.current!; dialog.showModal(); return () => dialog.close(); }, []);
  return <dialog ref={ref} className="modal" aria-labelledby={labelledBy} onCancel={(event) => { event.preventDefault(); onClose(); }}>{children}</dialog>;
}
