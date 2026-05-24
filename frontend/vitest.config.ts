import path from "node:path";
import { defineConfig } from "vitest/config";

// Vitest config for the Next.js frontend.
//
// We deliberately stick to the *node* environment for now: the
// rendering-tier (testing-library + jsdom) is not yet a dependency of
// this package, so render tests would not run. The current suite
// exercises:
//   - the typed REST client (URL composition, headers, methods)
//   - pure data-transform helpers used by components
// Once @testing-library/react + jsdom land we will switch
// ``environment`` to "jsdom" and add the DOM tests for components
// that today are validated only by hand.
export default defineConfig({
  test: {
    environment: "node",
    include: ["__tests__/**/*.test.ts", "__tests__/**/*.test.tsx"],
    globals: false,
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
});
