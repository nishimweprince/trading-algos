//! A small columnar frame, written to and read from parquet, so research
//! outputs open in pandas as before.

use std::fs::File;
use std::path::Path;
use std::sync::Arc;

use arrow_array::{Array, ArrayRef, BooleanArray, Float64Array, Int64Array, RecordBatch, StringArray};
use arrow_schema::{DataType, Field, Schema};
use indexmap::IndexMap;
use parquet::arrow::ArrowWriter;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use serde_json::Value;

/// A row whose keys keep their insertion order.
pub type Row = IndexMap<String, Value>;

#[derive(Clone, Debug, PartialEq)]
pub enum Col {
    I64(Vec<Option<i64>>),
    F64(Vec<Option<f64>>),
    Str(Vec<Option<String>>),
    Bool(Vec<Option<bool>>),
}

impl Col {
    pub fn len(&self) -> usize {
        match self {
            Col::I64(v) => v.len(),
            Col::F64(v) => v.len(),
            Col::Str(v) => v.len(),
            Col::Bool(v) => v.len(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn f64(&self, i: usize) -> Option<f64> {
        match self {
            Col::I64(v) => v[i].map(|x| x as f64),
            Col::F64(v) => v[i],
            Col::Bool(v) => v[i].map(|b| if b { 1.0 } else { 0.0 }),
            Col::Str(_) => None,
        }
    }

    pub fn i64(&self, i: usize) -> Option<i64> {
        match self {
            Col::I64(v) => v[i],
            Col::F64(v) => v[i].map(|x| x as i64),
            _ => None,
        }
    }

    pub fn str(&self, i: usize) -> Option<&str> {
        match self {
            Col::Str(v) => v[i].as_deref(),
            _ => None,
        }
    }

    pub fn value(&self, i: usize) -> Value {
        match self {
            Col::I64(v) => v[i].map(Value::from).unwrap_or(Value::Null),
            Col::F64(v) => v[i].map(Value::from).unwrap_or(Value::Null),
            Col::Str(v) => v[i].clone().map(Value::from).unwrap_or(Value::Null),
            Col::Bool(v) => v[i].map(Value::from).unwrap_or(Value::Null),
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Frame {
    pub cols: IndexMap<String, Col>,
}

impl Frame {
    pub fn len(&self) -> usize {
        self.cols.values().next().map(Col::len).unwrap_or(0)
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn col(&self, name: &str) -> &Col {
        self.cols.get(name).unwrap_or_else(|| panic!("no column {name}"))
    }

    pub fn push(&mut self, name: &str, col: Col) {
        self.cols.insert(name.to_owned(), col);
    }

    /// Rows as JSON objects (column order kept).
    pub fn rows(&self) -> Vec<serde_json::Map<String, Value>> {
        (0..self.len()).map(|i| self.cols.iter().map(|(k, c)| (k.clone(), c.value(i))).collect()).collect()
    }

    /// From JSON rows, as `pandas.DataFrame(rows)`: columns in first-seen
    /// order, a missing key is null, the type from the values (any float makes
    /// a numeric column float). `serde_json::Map` sorts its keys, so rows
    /// whose column order matters are [`Row`]s ([`Frame::from_records`]).
    pub fn from_rows(rows: &[serde_json::Map<String, Value>]) -> Self {
        let rows: Vec<Row> =
            rows.iter().map(|r| r.iter().map(|(k, v)| (k.clone(), v.clone())).collect()).collect();
        Self::from_records(&rows)
    }

    /// From ordered rows, as `pandas.DataFrame(rows)`.
    pub fn from_records(rows: &[Row]) -> Self {
        let mut names: IndexMap<String, ()> = IndexMap::new();
        for r in rows {
            for k in r.keys() {
                names.insert(k.clone(), ());
            }
        }
        let mut f = Frame::default();
        for name in names.keys() {
            let vals: Vec<&Value> = rows.iter().map(|r| r.get(name).unwrap_or(&Value::Null)).collect();
            let col = if vals.iter().any(|v| v.is_string()) {
                Col::Str(vals.iter().map(|v| v.as_str().map(str::to_owned)).collect())
            } else if vals.iter().any(|v| v.is_boolean()) {
                Col::Bool(vals.iter().map(|v| v.as_bool()).collect())
            } else if vals.iter().all(|v| v.is_null() || v.is_i64() || v.is_u64())
                && vals.iter().any(|v| !v.is_null())
            {
                if vals.iter().any(|v| v.is_null()) {
                    // pandas turns an int column with nulls into float.
                    Col::F64(vals.iter().map(|v| v.as_f64()).collect())
                } else {
                    Col::I64(vals.iter().map(|v| v.as_i64()).collect())
                }
            } else {
                Col::F64(vals.iter().map(|v| v.as_f64()).collect())
            };
            f.cols.insert(name.clone(), col);
        }
        f
    }

    pub fn write_parquet(&self, path: &Path) -> anyhow::Result<()> {
        let mut fields = Vec::new();
        let mut arrays: Vec<ArrayRef> = Vec::new();
        for (name, c) in &self.cols {
            let (dt, arr): (DataType, ArrayRef) = match c {
                Col::I64(v) => (DataType::Int64, Arc::new(Int64Array::from(v.clone()))),
                Col::F64(v) => (DataType::Float64, Arc::new(Float64Array::from(v.clone()))),
                Col::Str(v) => (DataType::Utf8, Arc::new(StringArray::from(v.clone()))),
                Col::Bool(v) => (DataType::Boolean, Arc::new(BooleanArray::from(v.clone()))),
            };
            fields.push(Field::new(name, dt, true));
            arrays.push(arr);
        }
        let schema = Arc::new(Schema::new(fields));
        let mut w = ArrowWriter::try_new(File::create(path)?, schema.clone(), None)?;
        if !arrays.is_empty() {
            w.write(&RecordBatch::try_new(schema, arrays)?)?;
        }
        w.close()?;
        Ok(())
    }

    pub fn read_parquet(path: &Path) -> anyhow::Result<Self> {
        let builder = ParquetRecordBatchReaderBuilder::try_new(File::open(path)?)?;
        let schema = builder.schema().clone();
        let mut f = Frame::default();
        for field in schema.fields() {
            let empty = match field.data_type() {
                DataType::Int64 | DataType::Int32 | DataType::Int16 | DataType::Int8 => Col::I64(Vec::new()),
                DataType::Float64 | DataType::Float32 => Col::F64(Vec::new()),
                DataType::Boolean => Col::Bool(Vec::new()),
                _ => Col::Str(Vec::new()),
            };
            f.cols.insert(field.name().clone(), empty);
        }
        for batch in builder.build()? {
            let batch = batch?;
            for (i, field) in schema.fields().iter().enumerate() {
                let arr = batch.column(i);
                let col = f.cols.get_mut(field.name()).expect("declared");
                append(col, arr.as_ref())?;
            }
        }
        Ok(f)
    }
}

fn append(col: &mut Col, arr: &dyn Array) -> anyhow::Result<()> {
    use arrow_array::cast::AsArray;
    use arrow_array::types::{Float32Type, Float64Type, Int8Type, Int16Type, Int32Type, Int64Type};
    let n = arr.len();
    let valid = |i: usize| arr.is_valid(i);
    match (col, arr.data_type()) {
        (Col::I64(v), DataType::Int64) => {
            v.extend((0..n).map(|i| valid(i).then(|| arr.as_primitive::<Int64Type>().value(i))))
        }
        (Col::I64(v), DataType::Int32) => {
            v.extend((0..n).map(|i| valid(i).then(|| arr.as_primitive::<Int32Type>().value(i) as i64)))
        }
        (Col::I64(v), DataType::Int16) => {
            v.extend((0..n).map(|i| valid(i).then(|| arr.as_primitive::<Int16Type>().value(i) as i64)))
        }
        (Col::I64(v), DataType::Int8) => {
            v.extend((0..n).map(|i| valid(i).then(|| arr.as_primitive::<Int8Type>().value(i) as i64)))
        }
        (Col::F64(v), DataType::Float64) => {
            v.extend((0..n).map(|i| valid(i).then(|| arr.as_primitive::<Float64Type>().value(i))))
        }
        (Col::F64(v), DataType::Float32) => {
            v.extend((0..n).map(|i| valid(i).then(|| arr.as_primitive::<Float32Type>().value(i) as f64)))
        }
        (Col::Bool(v), DataType::Boolean) => {
            v.extend((0..n).map(|i| valid(i).then(|| arr.as_boolean().value(i))))
        }
        (Col::Str(v), DataType::Utf8) => {
            v.extend((0..n).map(|i| valid(i).then(|| arr.as_string::<i32>().value(i).to_owned())))
        }
        (Col::Str(v), DataType::LargeUtf8) => {
            v.extend((0..n).map(|i| valid(i).then(|| arr.as_string::<i64>().value(i).to_owned())))
        }
        (_, dt) => anyhow::bail!("unsupported parquet column type {dt}"),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn round_trips_through_parquet() {
        let rows: Vec<serde_json::Map<String, Value>> = [
            json!({"a": 1, "b": 0.5, "c": "x", "d": true}),
            json!({"a": 2, "b": null, "c": null, "d": false, "e": 3}),
        ]
        .into_iter()
        .map(|v| v.as_object().unwrap().clone())
        .collect();
        let f = Frame::from_rows(&rows);
        assert_eq!(f.col("e"), &Col::F64(vec![None, Some(3.0)]));
        let p = std::env::temp_dir().join(format!("pk-frame-{}.parquet", std::process::id()));
        f.write_parquet(&p).unwrap();
        assert_eq!(Frame::read_parquet(&p).unwrap(), f);
        std::fs::remove_file(p).ok();
    }
}
