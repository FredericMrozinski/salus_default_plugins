import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../shared/salus";
import { queryClient } from "../shared/hooks";
import { RootGuard } from "../shared/settings";
import { App } from "../viewer/App";
import "../viewer/viewer.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RootGuard>
        <App />
      </RootGuard>
    </QueryClientProvider>
  </StrictMode>,
);
