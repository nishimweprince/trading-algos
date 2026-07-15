import { AppConfigService } from './src/config/app-config.service';
import { OpenAiVisionService } from './src/openai/openai-vision.service';

async function main() {
  const config = new AppConfigService();
  config.load({
    ...process.env,
    SOURCES: JSON.stringify([
      { type: 'TRADING_CENTRAL', url: 'https://example.com/tc' },
    ]),
  });

  const screenshotPath =
    './data/screenshots/TRADING_CENTRAL_2026-07-14T09-45-12-547Z.png';

  const vision = new OpenAiVisionService(config);
  console.log('Calling OpenAI with model:', config.openaiModel);
  const result = await vision.extract(screenshotPath, {
    provider: 'TRADING_CENTRAL',
    sourceUrl: 'https://example.com/tc',
    capturedAt: new Date().toISOString(),
    screenshotPath,
  });

  console.log('Ideas:', JSON.stringify(result.ideas, null, 2));
  console.log('Rejected:', JSON.stringify(result.rejected, null, 2));
  console.log('Model used:', result.model);
}

main()
  .catch((err) => {
    console.error('SMOKE TEST FAILED:', err);
    process.exitCode = 1;
  });
