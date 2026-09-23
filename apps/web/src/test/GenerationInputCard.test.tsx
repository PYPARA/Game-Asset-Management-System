import {render,screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {it,expect,vi} from 'vitest';
import {GenerationInputCard} from '../components/GenerationInputCard';
import type {GenerationInputRequest} from '../types';
it('answered questions can be expanded and collapsed repeatedly without resubmission',async()=>{
 const onAnswer=vi.fn();
 const request={id:'answered',status:'resolved',answers:{role:{answers:['文官']}},questions:[{id:'role',header:'身份',question:'角色身份是什么？',options:null}],response_mode:'resume_turn'} as unknown as GenerationInputRequest;
 render(<GenerationInputCard request={request} onAnswer={onAnswer}/>);
 const user=userEvent.setup();
 for(let i=0;i<2;i++){
  await user.click(screen.getByRole('button',{name:'查看回答'}));
  expect(screen.getByText('角色身份是什么？')).toBeVisible();
  await user.click(screen.getByRole('button',{name:'收起回答'}));
  expect(screen.queryByText('角色身份是什么？')).not.toBeInTheDocument();
 }
 expect(onAnswer).not.toHaveBeenCalled();
});
