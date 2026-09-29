from abc import ABC, abstractmethod

from typing import List, Dict, Any


class BaseChunkStrategy(ABC):
    """
    所有chunk策略基类
    """

    @abstractmethod
    def split(
            self,
            documents: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        输入:

        documents


        输出:

        chunks

        """

        pass
