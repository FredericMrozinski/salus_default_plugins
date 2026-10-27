import { useQuery } from "@tanstack/react-query";
import { ReactNode } from "react";
import { api } from "./salus";
import { Empty, ErrorLine } from "./ui";

/** The workbench folder: the `root` setting of this plugin, which the user sets in Salus' settings. */
export type Settings = {
  root: string | null; // what is set
  active: boolean; // whether the backend uses it (false if nothing is set, or the set folder cannot be used)
  error: string | null; // why the set folder cannot be used
};

export function useSettings() {
  // Polled: the setting is changed in another plugin (Settings), and this page should notice without a reload.
  return useQuery({ queryKey: ["settings"], queryFn: () => api<Settings>("GET", "/settings"), staleTime: 0, refetchInterval: 3000 });
}

/** Shows the app once the workbench folder is set up, and says where to set it before that. */
export function RootGuard({ children }: { children: ReactNode }) {
  const settings = useSettings();
  if (settings.isLoading) return <Empty>Loading…</Empty>;
  if (settings.error) return <ErrorLine error={settings.error} />;
  if (!settings.data?.active) {
    return (
      <div className="setup">
        <div className="setup-notice">
          <h2>{settings.data?.root ? "The workbench folder cannot be used" : "The workbench folder is not set yet"}</h2>
          {settings.data?.error && <div className="warn-line">{settings.data.error}</div>}
          <p>
            Go to <b>Settings → Dataset Workbench</b> and enter the folder on the server where the workbench keeps its data. This page continues by
            itself as soon as the folder is set.
          </p>
        </div>
      </div>
    );
  }
  return <>{children}</>;
}
