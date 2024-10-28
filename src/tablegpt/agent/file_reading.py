from __future__ import annotations

import logging
from ast import literal_eval
from enum import Enum
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from pybox.base import BasePyBoxManager  # noqa: TCH002

from tablegpt.chains.data_normalizer import (
    get_data_normalize_chain,
    get_table_reformat_chain,
    wrap_normalize_code,
)
from tablegpt.errors import NoAttachmentsError
from tablegpt.tools import IPythonTool, markdown_console_template
from tablegpt.utils import get_raw_table_info

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.language_models import BaseLanguageModel

logger = logging.getLogger(__name__)


class Stage(Enum):
    UPLOADED = 0
    INFO_READ = 1
    HEAD_READ = 2


class AgentState(MessagesState):
    # The message that we received from the user, act as an entry point
    entry_message: BaseMessage
    processing_stage: Stage
    # This is a bit of a hack to pass parent id to the agent state
    # But it act as the group id of all messages generated by the agent
    parent_id: str | None


ENCODER_INPUT_SEG_NUM = 2


def create_file_reading_workflow(
    pybox_manager: BasePyBoxManager,
    *,
    workdir: Path | None = None,
    session_id: str | None = None,
    nlines: int | None = None,
    model_type: str | None = None,
    enable_normalization: bool = False,
    normalize_llm: BaseLanguageModel | None = None,
    verbose: bool = False,
):
    """_summary_

    Args:
        nlines (int): _description_
        var_name (str): _description_
        pybox_manager (BasePyBoxManager): _description_
        workdir (Path | None, optional): _description_. Defaults to None.
        session_id (str | None, optional): _description_. Defaults to None.
        model_type (str | None, optional): _description_. Defaults to None.
        enable_normalization (bool, optional): _description_. Defaults to False.
        normalize_llm (BaseLanguageModel | None, optional): _description_. Defaults to None.
        verbose (bool, optional): _description_. Defaults to False.
    """
    if nlines is None:
        nlines = 5

    tools = [IPythonTool(pybox_manager=pybox_manager, cwd=workdir, session_id=session_id)]
    tool_executor = ToolNode(tools)

    async def agent(state: AgentState) -> dict:
        if state.get("processing_stage", Stage.UPLOADED) == Stage.UPLOADED:
            return await get_df_info(state)
        if state.get("processing_stage", Stage.UPLOADED) == Stage.INFO_READ:
            return get_df_head(state)

        return get_final_answer(state)

    async def generate_normalization_code(state: AgentState) -> str:
        if attachments := state["entry_message"].additional_kwargs.get("attachments"):
            # TODO: we only support one file for now
            filename = attachments[0].filename
        else:
            raise NoAttachmentsError

        filepath = workdir.joinpath(filename)
        var_name = state["entry_message"].additional_kwargs.get("var_name", "df")

        # TODO: refactor the data normalization to langgraph
        raw_table_info = get_raw_table_info(filepath=filepath)
        table_reformat_chain = get_table_reformat_chain(llm=normalize_llm)
        reformatted_table = await table_reformat_chain.ainvoke(input={"table": raw_table_info})

        if reformatted_table == raw_table_info:
            return ""

        normalize_chain = get_data_normalize_chain(llm=normalize_llm)
        normalization_code: str = await normalize_chain.ainvoke(
            input={
                "table": raw_table_info,
                "reformatted_table": reformatted_table,
            }
        )
        # Add try-except block to catch any errors that may occur during normalization.
        return wrap_normalize_code(var_name, normalization_code)

    async def get_df_info(state: AgentState) -> dict:
        if attachments := state["entry_message"].additional_kwargs.get("attachments"):
            # TODO: we only support one file for now
            filename = attachments[0].filename
        else:
            raise NoAttachmentsError

        var_name = state["entry_message"].additional_kwargs.get("var_name", "df")

        thought = f"我已经收到您的数据文件，我需要查看文件内容以对数据集有一个初步的了解。首先我会读取数据到 `{var_name}` 变量中，并通过 `{var_name}.info` 查看 NaN 情况和数据类型。"  # noqa: RUF001
        read_df_code = f"""# Load the data into a DataFrame
{var_name} = read_df('{filename}')"""

        normalization_code = ""
        if enable_normalization and normalize_llm:
            try:
                normalization_code = await generate_normalization_code(state)
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to generate normalization code: %s", str(e))

        tool_input = f"""{read_df_code}
{normalization_code}
# Remove leading and trailing whitespaces in column names
{var_name}.columns = {var_name}.columns.str.strip()

# Remove rows and columns that contain only empty values
{var_name} = {var_name}.dropna(how='all').dropna(axis=1, how='all')

# Get the basic information of the dataset
{var_name}.info(memory_usage=False)"""

        content = f"{thought}\n```python\n{tool_input}\n```"

        return {
            "messages": [
                AIMessage(
                    id=str(uuid4()),
                    content=content,
                    tool_calls=[
                        {
                            "name": "python",
                            "args": {"query": tool_input},
                            "id": str(uuid4()),
                        }
                    ],
                    additional_kwargs={
                        "parent_id": state["parent_id"],
                        "thought": thought,
                        "action": {
                            "tool": "python",
                            "tool_input": tool_input,
                        },
                        "model_type": model_type,
                    },
                )
            ],
            "processing_stage": Stage.INFO_READ,
        }

    def get_df_head(state: AgentState) -> dict:
        var_name = state["entry_message"].additional_kwargs.get("var_name", "df")

        thought = f"""接下来我将用 `{var_name}.head({nlines})` 来查看数据集的前 {nlines} 行。"""

        # The input visible to the LLM can prevent it from blindly imitating the actions of our encoder.
        default_tool_input = f"""# Show the first {nlines} rows to understand the structure
{var_name}.head({nlines})"""

        # Use the flush parameter to force a refresh of the buffer and return it to multiple text parts
        if model_type == "mm-tabular/markup":
            tool_input = f"""# Show the first {nlines} rows to understand the structure
print({var_name}.head({nlines}), flush=True)
print({var_name}.head(500).to_markdown(), flush=True)"""

        elif model_type == "mm-tabular/contrastive":
            tool_input = f"""# Show the first {nlines} rows to understand the structure
print({var_name}.head({nlines}), flush=True)
print(str(inspect_df({var_name})), flush=True)"""

        else:
            tool_input = default_tool_input

        # The input visible to the LLM can prevent it from blindly imitating the actions of our encoder.
        content = f"{thought}\n```python\n{default_tool_input}\n```"

        return {
            "messages": [
                AIMessage(
                    id=str(uuid4()),
                    content=content,
                    tool_calls=[
                        {
                            "name": "python",
                            "args": {"query": tool_input},
                            "id": str(uuid4()),
                        }
                    ],
                    additional_kwargs={
                        "parent_id": state["parent_id"],
                        "thought": thought,
                        "action": {
                            "tool": "python",
                            "tool_input": default_tool_input,
                        },
                        "model_type": model_type,
                    },
                )
            ],
            "processing_stage": Stage.HEAD_READ,
        }

    def get_final_answer(state: AgentState) -> dict:
        if attachments := state["entry_message"].additional_kwargs.get("attachments"):
            # TODO: we only support one file for now
            filename = attachments[0].filename
        else:
            raise NoAttachmentsError

        text = f"我已经了解了数据集 {filename} 的基本信息。请问我可以帮您做些什么？"  # noqa: RUF001
        return {
            "messages": [
                AIMessage(
                    id=str(uuid4()),
                    content=text,
                    additional_kwargs={
                        "parent_id": state["parent_id"],
                    },
                )
            ]
        }

    async def tool_node(state: AgentState) -> dict:
        messages: list[ToolMessage] = await tool_executor.ainvoke(state["messages"])
        for message in messages:
            message.additional_kwargs = message.additional_kwargs | {
                "parent_id": state["parent_id"],
                # Hide the execution results of the file upload tool.
                "display": False,
            }
            # TODO: this is very hard-coded to format encoder input like this.
            if (
                model_type in {"mm-tabular/markup", "mm-tabular/contrastive"}
                and len(message.content) == ENCODER_INPUT_SEG_NUM
            ):
                _df_head, _extra = message.content
                table_content = (
                    [literal_eval(_extra["text"])] if model_type == "mm-tabular/contrastive" else [_extra["text"]]
                )

                message.content = [
                    _df_head,
                    {"type": "table", "tables": table_content},
                ]
                message.additional_kwargs["hackfor"] = "encoder"
            for part in message.content:
                if isinstance(part, dict) and part.get("type") == "text":
                    part["text"] = markdown_console_template.format(res=part["text"])
        return {"messages": messages}

    def should_continue(state: AgentState) -> Literal["tools", "end"]:
        # Must have at least one message when entering this router
        last_message = state["messages"][-1]
        if last_message.tool_calls:
            return "tools"
        return "end"

    workflow = StateGraph(AgentState)

    workflow.add_node("agent", agent)
    workflow.add_node("tools", tool_node)

    workflow.add_edge(START, "agent")
    workflow.add_edge("tools", "agent")
    workflow.add_conditional_edges(
        "agent",
        should_continue,
        {
            "tools": "tools",
            "end": END,
        },
    )

    return workflow.compile(debug=verbose)
