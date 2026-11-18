import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      includeAssets: ["icons/icon-192.png", "icons/icon-512.png"],
      manifest: {
        name: "Finance Tracker",
        short_name: "Finance",
        description: "Personal spending and investing tracker",
        theme_color: "#1C1424",
        background_color: "#1C1424",
        display: "standalone",
        start_url: "/",
        icons: [
          {
            src: "icons/icon-192.png",
            sizes: "192x192",
            type: "image/png",
          },
          {
            src: "icons/icon-512.png",
            sizes: "512x512",
            type: "image/png",
          },
        ],
      },
      workbox: {
        // cache the app shell so it opens even with a flaky connection;
        // API calls to the Flask backend are NOT cached (always fresh data)
        runtimeCaching: [],
      },
    }),
  ],
  server: {
    host: "0.0.0.0", // reachable from your phone on the same WiFi
    port: 5173,
    // Same-origin API in dev too (production uses a Render rewrite), so the
    // HttpOnly session cookie works exactly as it does when deployed.
    proxy: {
      "/api": process.env.API_PROXY_TARGET || "http://localhost:5000",
    },
  },
});
