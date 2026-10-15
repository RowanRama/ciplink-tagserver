import type { ReactNode } from "react";
import { TagLink, useTagServer } from "@ciplink/react";
import { Browser, Motion, Overview } from "./pages";

/** A link to a hash route, for <TagLink as={HashLink}>. */
function HashLink({ to, children, ...rest }: { to: string; children?: ReactNode }) {
  return (
    <a href={`#${to}`} {...rest}>
      {children}
    </a>
  );
}

export function App({ page }: { page: string }) {
  const current = (path: string) => (page === path ? "page" : undefined);
  return (
    <>
      <header>
        <strong>Test Bench</strong>
        {/* while a TagLink is on screen, the tags its page showed before are read at tier 2 */}
        <nav>
          <TagLink to="/" as={HashLink} aria-current={current("/")}>
            Overview
          </TagLink>
          <TagLink to="/motion" as={HashLink} aria-current={current("/motion")}>
            Motion
          </TagLink>
          {/* a plain link: the browser subscribes whatever is expanded, nothing to prefetch */}
          <a href="#/browser" aria-current={current("/browser")}>
            Browser
          </a>
        </nav>
      </header>
      <main>{page === "/motion" ? <Motion /> : page === "/browser" ? <Browser /> : <Overview />}</main>
      <StatusBar />
    </>
  );
}

/** Fixed to the bottom of the window, so its changing text never moves a control. */
function StatusBar() {
  const { state, plcs, canWrite } = useTagServer();
  return (
    <footer className="status" data-state={state}>
      <span className="slot">server: {state}</span>
      {Object.entries(plcs).map(([name, s]) => (
        <span key={name} className="slot" data-plc-state={s}>
          {name}: {s}
        </span>
      ))}
      <span>{canWrite ? "writes allowed" : "read-only"}</span>
    </footer>
  );
}
