import { AppConfigService } from './src/config/app-config.service';
import { OcrService } from './src/ocr/ocr.service';
import { OllamaFormatterService } from './src/ollama/ollama-formatter.service';

async function main() {
  const config = new AppConfigService();
  config.load({
    SOURCES: JSON.stringify([
      { type: 'TRADING_CENTRAL', url: 'https://example.com/tc' },
    ]),
  });

  const ocrService = new OcrService();
  const screenshotPath =
    './data/screenshots/TRADING_CENTRAL_2026-07-14T09-45-12-547Z.png';

  console.log('Running OCR...');
  const ocr = await ocrService.recognize(screenshotPath);
  console.log('OCR confidence:', ocr.confidence);
  console.log('OCR text (first 300 chars):', ocr.text.slice(0, 300));

  const formatter = new OllamaFormatterService(config);
  console.log('Calling Ollama with model:', config.ollamaModel);
  const result = await formatter.format(ocr, {
    provider: 'TRADING_CENTRAL',
    sourceUrl: 'https://example.com/tc',
    capturedAt: new Date().toISOString(),
    screenshotPath,
  });

  console.log('Ideas:', JSON.stringify(result.ideas, null, 2));
  console.log('Rejected:', JSON.stringify(result.rejected, null, 2));
  console.log('Model used:', result.model, 'Repaired:', result.repaired);

  await ocrService.onModuleDestroy();
}

main()
  .catch((err) => {
    console.error('SMOKE TEST FAILED:', err);
    process.exitCode = 1;
  });
