import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Yellow Duck Labs reads as a quiet sentry, not an alarm panel.
        duck: {
          DEFAULT: "#f5b301",
          50: "#fffbea",
          200: "#fde68a",
          400: "#fbbf24",
          500: "#f5b301",
          600: "#d99500",
        },
        // Near-black, so hillshade and score colour carry the map.
        ink: {
          DEFAULT: "#070a0f",
          800: "#0b0e14",
          700: "#0d1117",
        },
      },
      fontFamily: {
        sans: ["ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
} satisfies Config;
