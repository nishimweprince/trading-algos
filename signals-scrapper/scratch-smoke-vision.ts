import 'dotenv/config';
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

  // Usage: npm run smoke:vision -- ./data/screenshots/<file>.png
  const screenshotPath = process.argv[2];
  if (!screenshotPath) {
    throw new Error('Pass a Trading Central screenshot path as the first argument.');
  }

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
