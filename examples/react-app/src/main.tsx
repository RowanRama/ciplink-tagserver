import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { TagServerProvider } from "@ciplink/react";
import { App } from "./App";

// matches [token.hmi1] in examples/tagserver.toml, which may write Line1.*.SpeedSP
const TOKEN = "change-me";

/** The route from the URL hash ("#/station1" -> "/station1"), so no router is needed. */
function useHashPath(): string {
  const read = () => location.hash.slice(1) || "/";
  const [path, setPath] = useState(read);
  useEffect(() => {
    const onChange = () => setPath(read());
    addEventListener("hashchange", onChange);
    return () => removeEventListener("hashchange", onChange);
  }, []);
  return path;
}

function Root() {
  const page = useHashPath();
  // page: the server learns which tags each page shows, so a <TagLink> to it can prefetch them
  return (
    <TagServerProvider url={`ws://${location.hostname}:8765/ws`} token={TOKEN} page={page}>
      <App page={page} />
    </TagServerProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
