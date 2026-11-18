import { defineConfig } from "vitepress";

export default defineConfig({
  title: "Solis",
  description: "Solis inverters in Home Assistant, from SolisCloud or a local stick",
  themeConfig: {
    nav: [
      { text: "Guide", link: "/" },
      { text: "Research", link: "/research/solis-api" },
    ],
    sidebar: [
      {
        text: "Guide",
        items: [
          { text: "Overview", link: "/" },
          { text: "Installation", link: "/installation" },
          { text: "Configuration", link: "/configuration" },
          { text: "Entities", link: "/entities" },
          { text: "Data updates", link: "/data-update" },
          { text: "Use cases", link: "/use-cases" },
          { text: "Examples", link: "/examples" },
          { text: "Limitations", link: "/limitations" },
          { text: "Troubleshooting", link: "/troubleshooting" },
          { text: "Removal", link: "/removal" },
          { text: "Lab screenshots", link: "/screenshots" },
        ],
      },
      {
        text: "Research",
        items: [
          { text: "Solis API and local access", link: "/research/solis-api" },
          { text: "Dongles and gateways", link: "/research/dongles" },
          { text: "Solarman local protocol", link: "/research/solarman-local" },
          { text: "S2-WL-ST local reads", link: "/research/s2-wl-st" },
          { text: "Solis web page stick", link: "/research/web-page-stick" },
        ],
      },
    ],
  },
});
