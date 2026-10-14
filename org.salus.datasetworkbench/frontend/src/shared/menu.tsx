import { MouseEvent, ReactNode, createContext, useCallback, useContext, useLayoutEffect, useRef, useState } from "react";
import { useDismiss } from "./hooks";

export type MenuItem =
  | { label: string; onSelect: () => void; disabled?: boolean; danger?: boolean; icon?: ReactNode }
  | "separator";

/** `at` opens the menu at a given spot (for example under a button) instead of at the pointer. */
type Show = (event: MouseEvent, items: MenuItem[], at?: { x: number; y: number }) => void;
const MenuContext = createContext<Show>(() => undefined);

/** `showMenu(event, items)` in an onContextMenu handler opens a context menu at the pointer. */
export const useMenu = () => useContext(MenuContext);

export function MenuProvider({ children }: { children: ReactNode }) {
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuItem[] } | null>(null);
  const show = useCallback<Show>((event, items, at) => {
    event.preventDefault();
    event.stopPropagation(); // the innermost item wins over its parents and the list background
    const visible = items.filter((item, i) => !(item === "separator" && (i === 0 || items[i - 1] === "separator" || i === items.length - 1)));
    if (visible.length) setMenu({ x: at?.x ?? event.clientX, y: at?.y ?? event.clientY, items: visible });
  }, []);
  return (
    <MenuContext.Provider value={show}>
      {children}
      {menu && <Menu {...menu} onClose={() => setMenu(null)} />}
    </MenuContext.Provider>
  );
}

function Menu({ x, y, items, onClose }: { x: number; y: number; items: MenuItem[]; onClose: () => void }) {
  const ref = useDismiss<HTMLDivElement>(onClose);
  const [pos, setPos] = useState({ left: x, top: y });
  const inner = useRef<HTMLDivElement | null>(null);
  // Keep the menu inside the (small) frame.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const { width, height } = el.getBoundingClientRect();
    setPos({ left: Math.max(2, Math.min(x, window.innerWidth - width - 2)), top: Math.max(2, Math.min(y, window.innerHeight - height - 2)) });
  }, [x, y, ref]);
  useLayoutEffect(() => {
    const close = () => onClose();
    window.addEventListener("blur", close);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("blur", close);
      window.removeEventListener("resize", close);
    };
  }, [onClose]);
  const anyIcon = items.some((item) => item !== "separator" && item.icon);
  return (
    <div className="context-menu" ref={ref} style={pos} role="menu" onContextMenu={(e) => e.preventDefault()}>
      <div ref={inner}>
        {items.map((item, i) =>
          item === "separator" ? (
            <div key={i} className="context-separator" />
          ) : (
            <button
              key={i}
              role="menuitem"
              className={`context-item ${item.danger ? "danger-text" : ""}`}
              disabled={item.disabled}
              onClick={() => {
                onClose();
                item.onSelect();
              }}
            >
              {item.icon ?? (anyIcon ? <span className="icon-spacer" /> : null)}
              <span>{item.label}</span>
            </button>
          ),
        )}
      </div>
    </div>
  );
}
