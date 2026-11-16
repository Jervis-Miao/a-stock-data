export default function ({ data, setTriggerValue }) {
    const storageKey = "a-stock-data.portfolio-columns";
    const { columns, editorKey } = data;
    const equal = (left, right) => left.every((value, index) => value === right[index]);
    let saved = [];
    try {
        const value = JSON.parse(localStorage.getItem(storageKey) || "[]");
        if (Array.isArray(value)) saved = value;
    } catch {
        // Browser storage is optional.
    }
    const restored = [...new Set([...saved.filter(name => columns.includes(name)), ...columns])];
    if (!equal(restored, columns)) setTriggerValue("order", restored);

    // Streamlit's canvas grid handles the drag. Read its committed column order
    // after the mouse is released, since Streamlit exposes no column-move event.
    function gridColumns(canvas) {
        const key = Object.keys(canvas).find(key => key.startsWith("__reactFiber$"));
        let fiber = canvas[key];
        let root = fiber;
        while (root?.return) root = root.return;
        if (root?.stateNode?.current !== root) fiber = fiber?.alternate ?? fiber;
        for (; fiber; fiber = fiber.return) {
            const props = fiber.memoizedProps;
            if (typeof props?.onColumnMoved !== "function" || !Array.isArray(props.columns)) continue;
            const names = props.columns.map(column => columns.includes(column.title) ? column.title : column.id)
                .filter(name => columns.includes(name));
            if (names.length >= 2) return names;
        }
        return [];
    }

    let drag;
    let pending;
    function pointerDown(event) {
        // Glide receives dragging on an overlay above the canvas.
        const editor = event.target.closest?.(".st-key-" + editorKey);
        const canvas = editor?.querySelector('[data-testid="data-grid-canvas"]');
        if (canvas) drag = { canvas, x: event.clientX };
    }
    function pointerUp(event) {
        const start = drag;
        drag = undefined;
        if (!start || Math.abs(event.clientX - start.x) < 5) return;
        pending = requestAnimationFrame(() => {
            pending = requestAnimationFrame(saveCommittedOrder);
        });
        function saveCommittedOrder() {
            const visible = gridColumns(start.canvas);
            const visibleSet = new Set(visible);
            const next = columns.map(name => visibleSet.has(name) ? visible.shift() : name);
            if (equal(next, columns)) return;
            try {
                const stored = [...next, ...saved.filter(name => !columns.includes(name))];
                localStorage.setItem(storageKey, JSON.stringify(stored));
            } catch {
                // Keep this session's table order even without browser storage.
            }
            setTriggerValue("order", next);
        }
    }
    let hoveredEditor;
    function updateCursor(event) {
        const editor = event.target.closest?.(".st-key-" + editorKey);
        const canvas = editor?.querySelector('[data-testid="data-grid-canvas"]');
        const bounds = canvas?.getBoundingClientRect();
        const overHeader = bounds && event.clientY >= bounds.top && event.clientY < bounds.top + 35
            && event.clientX >= bounds.left && event.clientX < bounds.right;
        if (hoveredEditor && (!overHeader || hoveredEditor !== editor)) {
            hoveredEditor.classList.remove("portfolio-header-hover");
            hoveredEditor = undefined;
        }
        if (overHeader && hoveredEditor !== editor) {
            hoveredEditor = editor;
            hoveredEditor.classList.add("portfolio-header-hover");
        }
    }
    document.addEventListener("pointermove", updateCursor, true);
    document.addEventListener("pointerdown", pointerDown, true);
    document.addEventListener("pointerup", pointerUp, true);
    return () => {
        document.removeEventListener("pointermove", updateCursor, true);
        hoveredEditor?.classList.remove("portfolio-header-hover");
        document.removeEventListener("pointerdown", pointerDown, true);
        document.removeEventListener("pointerup", pointerUp, true);
        if (pending !== undefined) cancelAnimationFrame(pending);
    };
}
