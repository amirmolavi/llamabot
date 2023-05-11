"""Class definition for QueryBot."""
import contextvars
from pathlib import Path
from typing import List, Union

from langchain.callbacks.base import CallbackManager
from langchain.callbacks.streaming_stdout import StreamingStdOutCallbackHandler
from langchain.chat_models import ChatOpenAI
from langchain.schema import AIMessage, HumanMessage, SystemMessage
from llama_index import GPTSimpleVectorIndex, LLMPredictor, ServiceContext
from loguru import logger

from llamabot.doc_processor import magic_load_doc, split_document
from llamabot.recorder import autorecord

prompt_recorder_var = contextvars.ContextVar("prompt_recorder")


class QueryBot:
    """QueryBot is a bot that lets us use GPT4 to query documents."""

    def __init__(
        self,
        system_message: str,
        model_name="gpt-4",
        temperature=0.0,
        doc_paths: List[Union[str, Path]] = None,
        saved_index_path: Union[str, Path] = None,
        chunk_size: int = 2000,
        chunk_overlap: int = 0,
    ):
        """Initialize QueryBot.

        Pass in either the doc_paths or saved_index_path to initialize the QueryBot.

        NOTE: QueryBot is not designed to have memory!

        The default text splitter is the TokenTextSplitter from LangChain.
        The default index that we use is the GPTSimpleVectorIndex from LlamaIndex.
        We also default to using GPT4 with temperature 0.0.

        :param system_message: The system message to send to the chatbot.
        :param model_name: The name of the OpenAI model to use.
        :param temperature: The model temperature to use.
            See https://platform.openai.com/docs/api-reference/completions/create#completions/create-temperature
            for more information.
        :param doc_paths: A list of paths to the documents to use for the chatbot.
            These are assumed to be plain text files.
        :param saved_index_path: The path to the saved index to use for the chatbot.
        :param chunk_size: The chunk size to use for the LlamaIndex TokenTextSplitter.
        :param chunk_overlap: The chunk overlap to use for the LlamaIndex TokenTextSplitter.
        """

        chat = ChatOpenAI(
            model_name=model_name,
            temperature=temperature,
            streaming=True,
            verbose=True,
            callback_manager=CallbackManager([StreamingStdOutCallbackHandler()]),
        )
        llm_predictor = LLMPredictor(llm=chat)
        service_context = ServiceContext.from_defaults(llm_predictor=llm_predictor)

        # Initialize index or load it from disk.
        index = GPTSimpleVectorIndex(nodes=[], service_context=service_context)
        if saved_index_path is not None:
            index = GPTSimpleVectorIndex.load_from_disk(
                saved_index_path, service_context=service_context
            )

        # Update index with new documents.
        if doc_paths is not None:
            index = insert_documents_into_index(
                doc_paths, index, chunk_size, chunk_overlap
            )

        else:
            logger.warning(
                "You've not provided any documents to index! Please be sure to do so later."
            )
            index = None

        # Set object attributes.
        self.system_message = system_message
        self.index = index
        self.doc_paths = doc_paths
        self.chat = chat
        self.chat_history = [
            SystemMessage(content=system_message),
            SystemMessage(
                content="""Do not hallucinate content.
If you cannot answer something, respond by saying that you don't know.
"""
            ),
        ]
        # Store a mapping of query to source nodes.
        self.source_nodes: dict = {}
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def __call__(
        self,
        query: str,
        similarity_top_k=3,
    ) -> AIMessage:
        """Call the QueryBot.

        :param query: The query to send to the document index.
        :param similarity_top_k: The number of documents to return from the index.
            These documents are added to the context of the chat history
            and then used to synthesize the response.
        :raises ValueError: if the index is not initialized.
        :return: The response to the query generated by GPT4.
        """
        if self.index is None:
            raise ValueError(
                "You need to provide a document for querybot to index against!"
            )
        # Step 1: Get documents from the index that are deemed to be matching the query.
        logger.info(f"Querying index for top {similarity_top_k} documents...")
        init_response = self.index.query(
            query, similarity_top_k=similarity_top_k, response_mode="no_text"
        )
        source_texts = [n.node.text for n in init_response.source_nodes]

        # Step 2: Construct a faux message history to work with.
        faux_chat_history = [SystemMessage(content=self.system_message)]
        faux_chat_history.append(
            SystemMessage(content="Here is the context you will be working with:")
        )
        for text in source_texts:
            faux_chat_history.append(SystemMessage(content=text))

        faux_chat_history.append(
            SystemMessage(content="Based on this context, answer the following query:")
        )

        faux_chat_history.append(HumanMessage(content=query))

        # Step 3: Send the chat history through the model
        response = self.chat(faux_chat_history)

        # Step 4: Record only the human response and the GPT response but not the original.
        self.chat_history.append(HumanMessage(content=query))
        self.chat_history.append(response)

        # Step 5: Record the source nodes of the query.
        self.source_nodes[query] = init_response.source_nodes

        autorecord(query, response.content)

        # Step 6: Return the response.
        return response

    def save(self, path: Union[str, Path]):
        """Save the QueryBot index to disk.

        :param path: The path to save the QueryBot index.
        """
        path = Path(path)
        if not path.suffix == ".json":
            path = path.with_suffix(".json")
        self.index.save_to_disk(path)

    def insert(self, file: Path):
        """Insert a document into the index.

        :param file: The path to the document to insert.
        """

        self.index = insert_documents_into_index(
            [file], self.index, self.chunk_size, self.chunk_overlap
        )


def insert_documents_into_index(
    doc_paths: List[Union[str, Path]],
    index: GPTSimpleVectorIndex,
    chunk_size: int,
    chunk_overlap: int,
):
    """Insert documents into the index.

    :param doc_paths: A list of paths to the documents to insert.
    :param index: The index to insert the documents into.
    :param chunk_size: The chunk size to use for the LangChain TokenTextSplitter.
    :param chunk_overlap: The chunk overlap to use for the LangChain TokenTextSplitter.
    :returns: The updated index.
    """
    for path in doc_paths:
        logger.info(f"Inserting {path} into the index...")
        document = magic_load_doc(path)
        docs = split_document(document, chunk_size, chunk_overlap)
        for doc in docs:
            index.insert(doc)
    return index
