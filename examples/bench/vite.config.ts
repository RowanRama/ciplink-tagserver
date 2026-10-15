import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// dedupe: the linked client folder has its own dev copy of React
export default defineConfig({ plugins: [react()], resolve: { dedupe: ["react", "react-dom"] } });
