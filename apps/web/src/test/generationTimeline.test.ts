import {it,expect} from "vitest";
import {compactConversationEvents} from "../components/GenerationChatPage";
import type {GenerationConversationEvent} from "../types";
it("commentary never hides the next answer and structured output shows progress",()=>{
 const base={session_id:"s",thread_id:"r",turn_id:"t",created_at:"2026-09-22"};
 const rows:GenerationConversationEvent[]=[
 {...base,id:"1",sequence:1,event_type:"assistant.delta",data:{item_id:"c",content:"先读项目"}},
 {...base,id:"2",sequence:2,event_type:"assistant.message",data:{item_id:"c",content:"先读项目",phase:"commentary"}},
 {...base,id:"3",sequence:3,event_type:"assistant.delta",data:{item_id:"final",content:'{"message":"方案"'}},
 ];
 const visible=compactConversationEvents(rows);
 expect(visible.filter(x=>x.event_type==="assistant.delta")).toHaveLength(1);
 expect(visible.at(-1)?.data.content).toContain("方案输出已接收");
 rows.push({...base,id:"4",sequence:4,event_type:"assistant.message",data:{item_id:"final",content:"方案已完成",phase:"final_answer"}});
 expect(compactConversationEvents(rows).filter(x=>x.event_type==="assistant.delta")).toHaveLength(0);
});
