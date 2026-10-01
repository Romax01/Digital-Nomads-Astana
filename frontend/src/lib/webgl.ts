/** Проверка WebGL (результат кэшируется: создание контекста — дорогая операция).
 *  При недоступности основной интерфейс работает в 2D. */
let cached: boolean | null = null;
export function hasWebGL(): boolean {
  if (cached !== null) return cached;
  try {
    const c = document.createElement("canvas");
    const gl = (c.getContext("webgl2") || c.getContext("webgl")) as WebGLRenderingContext | null;
    cached = !!(window.WebGLRenderingContext && gl);
    (gl?.getExtension("WEBGL_lose_context") as any)?.loseContext();
  } catch { cached = false; }
  return cached;
}
