import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  // StrictMode's development double-mount tears down drei <Html> overlay roots mid-render
  // (spurious React 19 warnings in the 3D twin); it has no effect on production builds.
  reactStrictMode: false,
  poweredByHeader: false,
  transpilePackages: ["three"],
};

export default nextConfig;
