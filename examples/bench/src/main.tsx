import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { TagServerProvider } from "@ciplink/react";
import { App } from "./App";
import "./style.css";

const url = import.meta.env.VITE_TAGSERVER_URL ?? "ws://127.0.0.1:8765/ws";
const token = import.meta.env.VITE_TAGSERVER_TOKEN ?? "bench-hmi"; // [token.hmi] in tagserver.toml

/** The hash route ("#/motion" -> "/motion"); the provider tells the server which page the tags belong to. */
function useHashPath() {
  const read = () => window.location.hash.slice(1) || "/";
  const [path, setPath] = useState(read);
  useEffect(() => {
    const onChange = () => setPath(read());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return path;
}

function Root() {
  const page = useHashPath();
  return (
    <TagServerProvider url={url} token={token} page={page}>
      <App page={page} />
    </TagServerProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
