import tensorflow as tf
from tensorflow.contrib.layers import \
    fully_connected, l2_regularizer, dropout, batch_norm
from typing import Dict

from tophat.embedding import EmbeddingMap
from tophat.utils.xn_utils import \
    preset_interactions, kernel_via_xn_sets, muls_via_xn_sets


class BilinearNet(object):
    """Network for scoring interactions

    Args:
        embedding_map: Variables and metadata concerning categorical embeddings
        interaction_type: Type of preset interaction
            One of {'intra', 'inter'}
    """

    def __init__(self,
                 embedding_map: EmbeddingMap,
                 interaction_type='inter',
                 ):
        self.embedding_map = embedding_map
        self.interaction_type = interaction_type

    def forward(self, input_xn_d: Dict[str, tf.Tensor]) -> tf.Tensor:
        """Forward inference step to score a user-item interaction
        
        Args:
            input_xn_d: Dictionary of feature names to category codes
                for a single interaction

        Returns:
            Forward inference scoring operation

        """

        # Handle sparse (embedding lookup of categorical features)
        embeddings_user, embeddings_item, embeddings_context, biases = \
            self.embedding_map.look_up(input_xn_d)

        embs_all = {**embeddings_user,
                    **embeddings_item,
                    **embeddings_context}

        fields_d = {
            'user': self.embedding_map.user_cat_cols,
            'item': self.embedding_map.item_cat_cols,
            'context': self.embedding_map.context_cat_cols,
        }

        interaction_sets = preset_interactions(
            fields_d, interaction_type=self.interaction_type)

        with tf.name_scope('interaction_model'):
            contrib_dot = kernel_via_xn_sets(interaction_sets, embs_all)
            # bias for cat feature factors
            contrib_bias = tf.add_n(list(biases.values()), name='contrib_bias')

        score = tf.add_n([contrib_dot, contrib_bias], name='score')

        return score


class BilinearNetWithNum(BilinearNet):
    """Forward inference step to score a user-item interaction
    With the ability to handle numerical (visual) features based on [1]_

    Args:
        embedding_map:Variables and metadata concerning categorical embeddings
        num_meta: Metadata concerning numerical data
            `feature_name -> dimensionality of input`
        l2_vis: l2 regularization scale for visual embedding matrix
        ruin: If True, use the formulation of [1]_
            Else, use a modified formulation
        interaction_type: Type of preset interaction
            One of {'intra', 'inter'}

    References:
        .. [1] He, Ruining, and Julian McAuley. "VBPR: Visual Bayesian 
           Personalized Ranking from Implicit Feedback." AAAI. 2016.

    """
    def __init__(self,
                 embedding_map: EmbeddingMap,
                 num_meta: Dict[str, int]=None,
                 l2_vis: float=0.,
                 ruin: bool=True,
                 interaction_type: str='inter',
                 ):
        BilinearNet.__init__(self, embedding_map, interaction_type)

        self.ruin = ruin
        self.num_meta = num_meta or {}
        # Params for numerical features
        # embedding matrix for each numerical feature (fully connected layer)
        self.l2_vis = l2_vis
        self.W_fc_num_d = {}
        self.b_fc_num_d = {}  # bias for fully connected
        self.b_num_factor_d = {}
        self.b_num_d = {}  # vbpr paper uses this shady bias matrix (beta')
        with tf.name_scope('numerical_reduction'):

            self.reg_vis = tf.contrib.layers.l2_regularizer(scale=self.l2_vis)
            K2 = self.embedding_map.embedding_dim
            for feat_name, dim_numerical in self.num_meta.items():
                # vbpr: E
                self.W_fc_num_d[feat_name] = tf.get_variable(
                    name=f'{feat_name}_fc_embedder',
                    shape=[dim_numerical, K2],
                    initializer=tf.random_normal_initializer(
                        stddev=1. / dim_numerical),
                    regularizer=self.reg_vis,
                )
                if not self.ruin:
                    # bias for E (not in paper)
                    self.b_fc_num_d[feat_name] = tf.get_variable(
                        name=f'{feat_name}_fc_bias',
                        shape=[self.embedding_map.embedding_dim],
                        initializer=tf.zeros_initializer(),
                    )
                    # just a scalar
                    self.b_num_factor_d[feat_name] = tf.get_variable(
                        name=f'{feat_name}_bias',
                        shape=[1],
                        initializer=tf.zeros_initializer(),
                    )
                else:
                    # vbpr: beta'
                    self.b_num_d[feat_name] = tf.get_variable(
                        name=f'{feat_name}_beta_prime',
                        shape=[dim_numerical],
                        initializer=tf.random_normal_initializer(
                            stddev=1. / dim_numerical),
                        regularizer=self.reg_vis,
                    )

    def forward(self, input_xn_d: Dict[str, tf.Tensor]) -> tf.Tensor:
        """Forward inference step to score a user-item interaction
        
        Args:
            input_xn_d: Dictionary of feature names to category codes
                for a single interaction

        Returns:
            Forward inference scoring operation

        """

        # Handle sparse (embedding lookup of categorical features)
        embeddings_user, embeddings_item, embeddings_context, biases = \
            self.embedding_map.look_up(
            input_xn_d)
        if self.embedding_map.vis_emb_user_col:
            emb_user_vis = tf.nn.embedding_lookup(
                self.embedding_map.user_vis,
                input_xn_d[self.embedding_map.vis_emb_user_col],
                name='user_vis_emb')
        else:
            emb_user_vis = None

        # Handle dense (fully connected reduction of dense features)
        # TODO: assume for now that all num feats are item-related
        #   (else, need extra book-keeping)
        user_num_cols = []
        if self.ruin:
            item_num_cols = []
        else:
            item_num_cols = list(self.num_meta.keys())
        if self.ruin:
            num_emb_d = {
                feat_name: tf.matmul(  # vbpr: theta_i
                    input_xn_d[feat_name], self.W_fc_num_d[feat_name],
                    name='item_vis_emb')
                # + self.b_fc_num_d[feat_name]  # fc bias (not in vbpr paper)
                for feat_name in self.num_meta.keys()
            }
        else:
            num_emb_d = {
                feat_name: tf.matmul(  # vbpr: theta_i
                    input_xn_d[feat_name], self.W_fc_num_d[feat_name],
                    name='item_vis_emb')
                + self.b_fc_num_d[feat_name]  # fc bias (not in vbpr paper)
                for feat_name in self.num_meta.keys()
            }

        # TODO: temp assume num are item features (not vbpr)
        embeddings_item.update(num_emb_d)

        embs_all = {**embeddings_user,
                    **embeddings_item,
                    **embeddings_context}

        fields_d = {
            'user': self.embedding_map.user_cat_cols + user_num_cols,
            'item': self.embedding_map.item_cat_cols + item_num_cols,
        }

        interaction_sets = preset_interactions(
            fields_d, interaction_type=self.interaction_type)

        with tf.name_scope('interaction_model'):
            contrib_dot = kernel_via_xn_sets(interaction_sets, embs_all)
            # bias for cat feature factors
            if len(biases.values()):
                contrib_bias = tf.add_n(list(biases.values()),
                                        name='contrib_bias')
            else:
                contrib_bias = tf.zeros_like(contrib_dot,
                                             name='contrib_bias')

            if self.b_num_factor_d.values():
                # bias for num feature factors
                contrib_bias += tf.add_n(list(self.b_num_factor_d.values()))
            # NOTE: vbpr paper uses a bias matrix beta that we take a
            #   dot product with original numerical
            if self.b_num_d:
                contrib_vis_bias = tf.add_n(  # vbpr: beta * f
                    [tf.reduce_sum(
                        tf.multiply(input_xn_d[feat_name], self.b_num_d[feat_name]),
                        1, keep_dims=False
                    ) for feat_name in self.num_meta.keys()],
                    name='contrib_vis_bias'
                )
            else:
                contrib_vis_bias = tf.zeros_like(contrib_bias,
                                                 name='contrib_vis_bias')

            # TODO: manually create visual interaction
            if len(num_emb_d):
                contrib_vis_dot = tf.add_n([
                    tf.reduce_sum(
                        # theta_u.T * theta_i
                        tf.multiply(emb_user_vis, num_emb), 1, keep_dims=False)
                    for feat_name, num_emb in num_emb_d.items()
                ], name='contrib_vis_dot')
            else:
                contrib_vis_dot = tf.zeros_like(contrib_bias,
                                                name='contrib_vis_dot')

            score = tf.add_n([contrib_dot,
                              contrib_bias,
                              contrib_vis_dot,
                              contrib_vis_bias],
                             name='score')
        return score


class BilinearNetWithNumFC(BilinearNet):
    """POC to replace the inner product potion with FC layers as described in
    [2]_ and [3]_

    Args:
        embedding_map: Variables and metadata concerning categorical embeddings
        num_meta: Metadata concerning numerical data
            `feature_name -> dimensionality of input`
        l2: l2 regularization scale of deep portion
        interaction_type: Type of preset interaction
            One of {'intra', 'inter'}

    References:
        .. [2] He, Xiangnan, et al. "Neural collaborative filtering." 
           Proceedings of the 26th International Conference on World Wide 
           Web. International World Wide Web Conferences Steering 
           Committee, 2017.
        
        .. [3] Xiangnan He and Tat-Seng Chua (2017). Neural Factorization 
           Machines for Sparse Predictive Analytics. In Proceedings of 
           SIGIR '17, Shinjuku, Tokyo, Japan, August 07-11, 2017.
    """

    def __init__(self,
                 embedding_map: EmbeddingMap,
                 num_meta: Dict[str, int]=None,
                 l2=1e0,
                 interaction_type='inter',
                 ):
        BilinearNet.__init__(self, embedding_map, interaction_type)

        self.num_meta = num_meta or {}
        self.regularizer = l2_regularizer(l2)

    def forward(self, input_xn_d: Dict[str, tf.Tensor]) -> tf.Tensor:
        """Forward inference step to score a user-item interaction
        
        Args:
            input_xn_d: Dictionary of feature names to category codes
                for a single interaction

        Returns:
            Forward inference scoring operation

        """

        # Handle sparse (embedding lookup of categorical features)
        embeddings_user, embeddings_item, embeddings_context, biases = self.embedding_map.look_up(
            input_xn_d)
        # bias not used

        num_emb_d = {
            feat_name: input_xn_d[feat_name] for feat_name in self.num_meta.keys()
        }

        embs_all = {**embeddings_user,
                    **embeddings_item,
                    **embeddings_context,
                    **num_emb_d}

        fields_d = {
            'user': self.embedding_map.user_cat_cols,
            'item': self.embedding_map.item_cat_cols,
        }

        interaction_sets = preset_interactions(
            fields_d, interaction_type=self.interaction_type)

        with tf.name_scope('interaction_model'):
            V = muls_via_xn_sets(interaction_sets, embs_all)
            f_bi = tf.add_n([node for s, node in V.items() if len(s) > 1],
                            name='f_bi')

        with tf.name_scope('deep'):
            # x = tf.concat([embs_all[k] for k in sorted(embs_all)], axis=1)
            x = tf.identity(f_bi, name='x')

            bn0 = batch_norm(x, decay=0.9)
            drop0 = dropout(bn0, 0.5)

            fc1 = fully_connected(drop0, 8, activation_fn=tf.nn.relu,
                                  weights_regularizer=self.regularizer)
            bn1 = batch_norm(fc1, decay=0.9)
            drop1 = dropout(bn1, 0.8)
            fc2 = fully_connected(drop1, 4, activation_fn=tf.nn.relu,
                                  weights_regularizer=self.regularizer)
            bn2 = batch_norm(fc2, decay=0.9)
            drop2 = dropout(bn2, 0.8)
            contrib_f = tf.squeeze(
                fully_connected(drop2, 1, activation_fn=None),
                name='score')

            contrib_bias = tf.add_n(list(biases.values()), name='contrib_bias')

        score = tf.add_n([contrib_f, contrib_bias], name='score')

        return score

