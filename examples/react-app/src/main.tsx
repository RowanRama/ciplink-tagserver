import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { TagServerProvider } from "@ciplink/react";
import { App } from "./App";

// matches [token.hmi1] in examples/tagserver.toml, which may write Line1.*.SpeedSP
const TOKEN = "change-me";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <TagServerProvider url={`ws://${location.hostname}:8765/ws`} token={TOKEN}>
      <App />
    </TagServerProvider>
  </StrictMode>,
);
