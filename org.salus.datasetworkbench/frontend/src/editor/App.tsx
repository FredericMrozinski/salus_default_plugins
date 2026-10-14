import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, encode, initialNav, onNav } from "../shared/salus";
import type { DatasetDetail, Nav } from "../shared/types";
import { Empty, ErrorLine } from "../shared/ui";
import { EntryForm } from "./EntryForm";
import { SchemaEditor } from "./SchemaEditor";

type View = { dataset: string | null; entry: number | null; mode: "entry" | "schema" };

export function App() {
  const [view, setView] = useState<View>({ dataset: null, entry: null, mode: "entry" });

  useEffect(() => {
    const apply = (nav: Nav) => {
      if (nav.type === "open_dataset") setView({ dataset: nav.dataset, entry: null, mode: "entry" });
      else if (nav.type === "show_entry") setView({ dataset: nav.dataset, entry: nav.entry, mode: "entry" });
      else setView({ dataset: nav.dataset, entry: null, mode: "schema" });
    };
    const initial = initialNav();
    if (initial) apply(initial);
    else
      api<{ dataset: string | null; entry: number | null }>("GET", "/state")
        .then((s) => s.dataset && setView({ dataset: s.dataset, entry: s.entry, mode: "entry" }))
        .catch(() => undefined);
    return onNav(apply);
  }, []);

  const detail = useQuery({
    queryKey: ["dataset", view.dataset],
    queryFn: () => api<DatasetDetail>("GET", `/datasets/${encode(view.dataset!)}`),
    enabled: !!view.dataset,
  });

  if (!view.dataset) return <Empty>Choose a dataset in the Workbench, then an entry to edit.</Empty>;
  if (detail.error) return <ErrorLine error={detail.error} />;
  if (!detail.data) return <Empty>Loading…</Empty>;

  if (view.mode === "schema")
    return (
      <SchemaEditor
        key={detail.data.id}
        dataset={detail.data}
        onDone={() => setView({ ...view, mode: "entry" })}
      />
    );
  return (
    <EntryForm
      dataset={detail.data}
      entry={view.entry}
      onEditSchema={() => setView({ ...view, mode: "schema" })}
      onClosed={() => setView({ ...view, entry: null })}
    />
  );
}
