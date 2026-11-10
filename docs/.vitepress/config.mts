import { defineConfig } from "vitepress";

export default defineConfig({
  title: "Solis",
  description: "SolisCloud for Home Assistant",
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
        ],
      },
      {
        text: "Research",
        items: [{ text: "Solis API and local access", link: "/research/solis-api" }],
      },
    ],
  },
});
