export interface SimpleMovingAverageProps {
    period: number;
    data: number[];
}
export interface MovingAverageResult {
    value: number;
    index: number;
}
export declare const simpleMovingAverage: ({ period, data, }: SimpleMovingAverageProps) => MovingAverageResult[];
export declare const calculateSMA: (data: number[], period: number) => number[];
