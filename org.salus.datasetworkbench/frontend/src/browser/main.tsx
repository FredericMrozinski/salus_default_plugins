import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../shared/salus";
import { queryClient } from "../shared/hooks";
import { MenuProvider } from "../shared/menu";
import { RootGuard } from "../shared/settings";
import { App } from "./App";
import { setOnBatchDone } from "./uploadQueue";
import "./browser.css";

// When a batch of uploads is over, what was uploaded shows up in the tree and the lists.
setOnBatchDone(() => {
  for (const key of ["tree", "files", "search"]) queryClient.invalidateQueries({ queryKey: [key] });
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <MenuProvider>
        <RootGuard>
          <App />
        </RootGuard>
      </MenuProvider>
    </QueryClientProvider>
  </StrictMode>,
);
