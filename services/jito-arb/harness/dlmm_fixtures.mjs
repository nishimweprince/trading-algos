// Offline independent oracle. Usage: node harness/dlmm_fixtures.mjs /path/to/pinned/dlmm-sdk
// npm install --ignore-scripts in that checkout's ts-client; npm run build.
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {execFileSync} from 'node:child_process';
const pin = '1532e540db41ac405dbf4c5bb2ddfceaa1f98ff1';
const root = path.resolve(process.argv[2]);
if (execFileSync('git', ['-C',root,'rev-parse','HEAD'],{encoding:'utf8'}).trim() !== pin) throw Error('wrong source pin');
const req = createRequire(path.join(root,'ts-client/package.json'));
const {Program, BN} = req('@coral-xyz/anchor');
const {PublicKey} = req('@solana/web3.js');
const sdk = req('./dist/index.js');
const idl = JSON.parse(fs.readFileSync(path.join(root,'ts-client/src/dlmm/idl/idl.json')));
const coder = new Program(idl, {connection:{}}).coder.accounts;
function encode(name, account) {
 // Anchor's public encode allocates only 1000 bytes; use its same IDL-derived layout at full size.
 const entry = coder.accountLayouts.get(name), body = Buffer.alloc(entry.layout.span);
 entry.layout.encode(account, body);
 return Buffer.concat([Buffer.from(entry.discriminator), body]);
}
const pairKey = 'FJbEo74c2W4QLBBVUfUvi8VBWXtMdJVPuFpq2f6UV1iB';
const folder = path.join(root,'artifacts',pairKey);
const pairArtifact = JSON.parse(fs.readFileSync(path.join(folder,pairKey+'.json')));
const pair = coder.decode('lbPair',Buffer.from(pairArtifact.account.data[0],'base64'));
function json(v) { return JSON.parse(JSON.stringify(v)); }
const accounts = [];
for (const f of fs.readdirSync(folder).sort()) {
 const a = JSON.parse(fs.readFileSync(path.join(folder,f))); const data = Buffer.from(a.account.data[0],'base64');
 if (![904,10136].includes(data.length)) continue;
 const type = data.length === 904 ? 'lbPair' : 'binArray';
 accounts.push({type, key:a.pubkey, owner:a.account.owner, data:data.toString('base64'), decoded:json(coder.decode(type,data)), provenance:'official artifacts/'+pairKey+'/'+f});
}
// Signed indices and nonzero order fields encoded by the pinned official IDL, without local offsets.
let bin = coder.decode('binArray',Buffer.from(accounts.find(a=>a.type==='binArray').data,'base64'));
bin.index = new BN(-513); bin.lbPair = new PublicKey(pairKey);
bin.bins[69].amountX = new BN('123456789'); bin.bins[69].amountY = new BN('987654321');
bin.bins[69].openOrderAmount = new BN(113); bin.bins[69].processedOrderRemainingAmount = new BN(79); bin.bins[69].limitOrderAskSide = 1;
accounts.push({type:'binArray', key:'synthetic-negative', owner:pairArtifact.account.owner, data:encode('binArray',bin).toString('base64'),decoded:json(bin),provenance:'official IDL encoded synthetic negative index / orders'});
const bitmap = {lbPair:new PublicKey(pairKey),positiveBinArrayBitmap:Array.from({length:12},()=>Array.from({length:8},()=>new BN(0))),negativeBinArrayBitmap:Array.from({length:12},()=>Array.from({length:8},()=>new BN(0)))};
bitmap.positiveBinArrayBitmap[0][0]=new BN(1); bitmap.positiveBinArrayBitmap[11][7]=new BN(1).shln(63);
bitmap.negativeBinArrayBitmap[0][0]=new BN(1); bitmap.negativeBinArrayBitmap[11][7]=new BN(1).shln(63);
accounts.push({type:'binArrayBitmapExtension',key:'synthetic-bitmap',owner:pairArtifact.account.owner,data:encode('binArrayBitmapExtension',bitmap).toString('base64'),decoded:json(bitmap),provenance:'official IDL encoded synthetic extension endpoints'});
const bitmapCases = [];
const bitmapPair = {...pair,binArrayBitmap:Array.from({length:16},()=>new BN(0))};
bitmapPair.binArrayBitmap[0]=new BN(1); bitmapPair.binArrayBitmap[15]=new BN(1).shln(63);
for (const from of [-6656,-6655,-1025,-1024,-513,-512,-511,-1,0,510,511,512,513,1023,6654,6655]) for (const down of [true,false]) {
 const next = sdk.findNextBinArrayIndexWithLiquidity(down,new BN(from*70),bitmapPair,bitmap);
 bitmapCases.push({from,down,next:next?.toNumber()??null});
}
const instructionCases=[];
const program = new Program(idl,{connection:{}});
const programId = program.programId;
for(const down of [true,false]) {
 const input = down ? pair.tokenXMint : pair.tokenYMint;
 const output = down ? pair.tokenYMint : pair.tokenXMint;
 const ix = await program.methods.swap2(new BN(12345),new BN(678),{slices:[]}).accountsStrict({
  lbPair:new PublicKey(pairKey),binArrayBitmapExtension:null,reserveX:pair.reserveX,reserveY:pair.reserveY,
  userTokenIn:input,userTokenOut:output,tokenXMint:pair.tokenXMint,tokenYMint:pair.tokenYMint,oracle:pair.oracle,
  hostFeeIn:null,user:PublicKey.default,tokenXProgram:new PublicKey('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'),
  tokenYProgram:new PublicKey('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'),memoProgram:new PublicKey('MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr'),
  eventAuthority:PublicKey.findProgramAddressSync([Buffer.from('__event_authority')],programId)[0],program:programId
 }).remainingAccounts([{pubkey:sdk.deriveBinArray(new PublicKey(pairKey),new BN(0),programId)[0],isSigner:false,isWritable:true}]).instruction();
 instructionCases.push({down,program:ix.programId.toBase58(),data:ix.data.toString('base64'),accounts:ix.keys.map(a=>({key:a.pubkey.toBase58(),signer:a.isSigner,writable:a.isWritable}))});
}
const prices = [-443636,-35910,-71,-70,-69,-1,0,1,69,70,35910,443636].map(id=>({id,step:1,price:sdk.getQPriceFromId(new BN(id),new BN(1)).toString()}));
const cases=[];
for (const active of [-71,-1,0,69]) for (const elapsed of [0,9,10,119,120]) for (const down of [true,false]) for (const mode of [0,1]) {
 const s={...pair.parameters,baseFactor:10000,filterPeriod:10,decayPeriod:120,reductionFactor:1000,variableFeeControl:40000,maxVolatilityAccumulator:1000000,baseFeePowerFactor:0,protocolShare:1500,collectFeeMode:mode,functionType:2};
 const v={...pair.vParameters,volatilityAccumulator:200000,volatilityReference:30000,indexReference:active+2,lastUpdateTimestamp:new BN(1000)};
 const context={...v}; sdk.updateReference(active,context,s,1000+elapsed); sdk.updateVolatilityAccumulator(context,s,active);
 const b={...bin.bins[69],amountX:new BN(11000),amountY:new BN(17000),openOrderAmount:new BN(7000),processedOrderRemainingAmount:new BN(3000),limitOrderAskSide:down?0:1,price:sdk.getQPriceFromId(new BN(active),new BN(25))};
 for (const amount of [1,999,20000,50000]) {
  const q=sdk.swapExactInQuoteAtBin(b,25,s,context,new BN(amount),down,true,mode===0||!down);
  cases.push({active,elapsed,down,mode,amount,price:b.price.toString(),rate:sdk.getTotalFee(25,s,context).toString(),amount_in:q.amountIn.toString(),amount_out:q.amountOut.toString(),fee:q.fee.add(q.protocolFee).toString()});
 }
}
// Decoder fixtures and per-bin SDK vectors are NOT mainnet simulation parity evidence.
fs.writeFileSync('tests/fixtures/dlmm_sdk.json',JSON.stringify({source:'MeteoraAg/dlmm-sdk',commit:pin,accounts,prices,cases,bitmapCases,instructionCases},null,2)+'\n');
console.log(`wrote ${accounts.length} decoder accounts, ${prices.length} prices, ${cases.length} per-bin SDK cases`);
