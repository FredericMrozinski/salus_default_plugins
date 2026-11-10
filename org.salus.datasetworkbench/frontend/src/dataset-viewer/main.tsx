import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "../shared/salus";
import { queryClient } from "../shared/hooks";
import { MenuProvider } from "../shared/menu";
import { RootGuard } from "../shared/settings";
import { App } from "../viewer/App";
import "../viewer/viewer.css";

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
