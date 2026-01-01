export const simpleMovingAverage = ({ period, data, }) => {
    if (period <= 0 || period > data.length) {
        throw new Error('Period must be greater than 0 and less than or equal to data length');
    }
    const results = [];
    for (let i = period - 1; i < data.length; i++) {
        const slice = data.slice(i - period + 1, i + 1);
        const sum = slice.reduce((acc, curr) => acc + curr, 0);
        const average = sum / period;
        results.push({
            value: average,
            index: i,
        });
    }
    return results;
};
export const calculateSMA = (data, period) => {
    if (period <= 0 || period > data.length) {
        throw new Error('Period must be greater than 0 and less than or equal to data length');
    }
    const smaValues = [];
    for (let i = period - 1; i < data.length; i++) {
        const slice = data.slice(i - period + 1, i + 1);
        const sum = slice.reduce((acc, curr) => acc + curr, 0);
        smaValues.push(sum / period);
    }
    return smaValues;
};
