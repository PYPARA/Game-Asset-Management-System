import { describe, it, expect } from "vitest";
import { ConversationProjection } from "../lib/generationStore";
import type { GenerationConversationEvent } from "../types";
const event = (sequence:number,type="assistant.delta"):GenerationConversationEvent => ({id:`e${sequence}`,sequence,session_id:"s",turn_id:"t",thread_id:null,event_type:type,data:{content:String(sequence)+",",item_id:"answer"},created_at:"2026-09-22"});
describe("durable conversation projection",()=>{
  it("deduplicates and orders ten thousand raw deltas without ten thousand rendered rows",()=>{
    const state=new ConversationProjection();
    for(let i=10000;i>=1;i--) {state.apply(event(i));state.apply(event(i));}
    expect(state.cursor).toBe(10000);
    const rows=state.values();
    expect(rows).toHaveLength(1);
    expect(rows[0].data.content).toBe(Array.from({length:10000},(_,i)=>`${i+1},`).join(""));
    expect(rows[0].sequence).toBe(1);
  });
  it("late history never regresses the cursor or drops newer messages",()=>{
    const state=new ConversationProjection();state.apply(event(600,"turn.completed"));
    for(let i=599;i>=1;i--)state.apply(event(i,"user.message"));
    expect(state.cursor).toBe(600);
    expect(state.values()).toHaveLength(600);
    expect(state.values().at(-1)?.event_type).toBe("turn.completed");
  });
});

it("keeps commentary and the subsequent final stream as separate items",()=>{
  const state=new ConversationProjection();
  state.apply({...event(1),data:{item_id:"commentary",content:"先读取项目"}});
  state.apply({...event(2,"assistant.message"),data:{item_id:"commentary",content:"先读取项目",phase:"commentary"}});
  state.apply({...event(3),data:{item_id:"final",content:'{"message":"正在整理"'}});
  expect(state.values().filter(x=>x.event_type==="assistant.delta")).toHaveLength(2);
});
