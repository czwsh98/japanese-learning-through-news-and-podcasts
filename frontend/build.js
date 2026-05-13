import { build } from 'vite';

try {
  await build();
} catch (err) {
  console.error("Vite build failed:", err.message);
  console.dir(err.errors, { depth: null });
}
