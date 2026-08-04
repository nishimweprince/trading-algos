import { api } from "@/lib/api";

export async function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

export async function uploadScreenshot(
  sessionId: string,
  kind: "entry" | "exit",
  blob: Blob,
  tradeId?: string,
): Promise<{ path: string; trade_id: string }> {
  const image_base64 = await blobToBase64(blob);
  return api.uploadScreenshot({
    session_id: sessionId,
    trade_id: tradeId,
    kind,
    image_base64,
  });
}
