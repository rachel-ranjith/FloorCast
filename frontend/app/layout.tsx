import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "FloorCast - Data Centre Floor Visualization",
  description: "Premium 3D digital twin visualization for data centre operations",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="bg-background">
      <body className="antialiased min-h-screen">
        {children}
      </body>
    </html>
  );
}
