const mockRecognize = jest.fn();
const mockSetParameters = jest.fn().mockResolvedValue(undefined);
const mockTerminate = jest.fn().mockResolvedValue(undefined);
const mockCreateWorker = jest.fn().mockResolvedValue({
  recognize: mockRecognize,
  setParameters: mockSetParameters,
  terminate: mockTerminate,
});

jest.mock('tesseract.js', () => ({
  createWorker: mockCreateWorker,
  OEM: { LSTM_ONLY: 1 },
  PSM: { AUTO: '3' },
}));

import { OcrService, tsvToPositionalText } from '../src/ocr/ocr.service';

describe('OcrService', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockSetParameters.mockResolvedValue(undefined);
    mockTerminate.mockResolvedValue(undefined);
    mockCreateWorker.mockResolvedValue({
      recognize: mockRecognize,
      setParameters: mockSetParameters,
      terminate: mockTerminate,
    });
  });

  it('converts TSV words into positioned reading-order lines', () => {
    const tsv = [
      'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext',
      '5\t1\t1\t1\t1\t1\t100\t20\t30\t10\t95\tAUD/JPY',
      '5\t1\t1\t1\t1\t2\t150\t20\t30\t10\t94\t30',
      '5\t1\t1\t1\t1\t3\t180\t20\t30\t10\t94\tMIN',
      '5\t1\t1\t1\t2\t1\t100\t50\t30\t10\t93\tTarget',
      '5\t1\t1\t1\t2\t2\t200\t50\t30\t10\t93\t113.06',
    ].join('\n');
    expect(tsvToPositionalText(tsv)).toContain(
      '[x=100,y=20] AUD/JPY 30 MIN',
    );
    expect(tsvToPositionalText(tsv)).toContain('[x=100,y=50] Target 113.06');
  });

  it('reuses one worker across screenshots and terminates it once', async () => {
    mockRecognize.mockResolvedValue({
      data: {
        text: 'AUD/JPY Target 113.06 Pivot 112.17',
        confidence: 91,
        tsv: '',
      },
    });
    const service = new OcrService();
    await service.recognize('/tmp/a.png');
    await service.recognize('/tmp/b.png');
    await service.onModuleDestroy();

    expect(mockCreateWorker).toHaveBeenCalledTimes(1);
    expect(mockRecognize).toHaveBeenCalledTimes(2);
    expect(mockTerminate).toHaveBeenCalledTimes(1);
  });

  it('fails explicitly when OCR returns no text', async () => {
    mockRecognize.mockResolvedValue({
      data: { text: '  ', confidence: 0, tsv: '' },
    });
    const service = new OcrService();
    await expect(service.recognize('/tmp/empty.png')).rejects.toThrow(
      /no text/i,
    );
    await service.onModuleDestroy();
  });
});
