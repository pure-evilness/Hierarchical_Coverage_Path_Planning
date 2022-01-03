from numpy import dtype
import torch
import torch.nn as nn
from Environment import Environment
from module import Pointer, AttentionModule, Glimpse
from torch.distributions import Categorical
from A_star import *
# define decoder
class Encoder(torch.nn.Module):
    def __init__(self, n_feature, n_hidden):
        super(Encoder, self).__init__()        
        self.n_hidden = n_hidden
        self.n_feature = n_feature
        self.embedding_x = nn.Linear(n_feature, n_hidden)                        
        self.high_node = []
        self.high_attention = AttentionModule(
                                            n_heads = 8,
                                            n_hidden = n_hidden,
                                            feed_forward_hidden= 512,
                                            n_layers = 3
                                            )

    def forward(self, batch_data, mask = None):    
        # cell embedding for high model        
        self.high_node = torch.tensor(()).cuda()
        self.original_node = torch.tensor(()).cuda()
        for index, batch_sample in enumerate(batch_data): 
            # data normalization
            A,B,C,D = batch_sample.size()
            batch_sample = batch_sample.reshape([A, B, C * D]).cuda()
            high_node = self.embedding_x(batch_sample.type(torch.float32).cuda() / 70.0)             
            high_node = self.high_attention(high_node) # size = [4 * n_cells, n_feature, n_hidden] 
            self.high_node = torch.cat([self.high_node, high_node], dim=1)            
            self.original_node = torch.cat([self.original_node, batch_sample], dim=1)
        return self.high_node.squeeze(0), self.original_node.squeeze(0)

class Decoder(torch.nn.Module):
    def __init__(self, n_embedding, n_hidden, n_head, C = 10):
        super(Decoder, self).__init__()
        self.n_embedding = n_embedding
        self.n_hidden = n_hidden        
        self.n_head = n_head
        self.C = C

        # define initial parameters
        self.init_w = nn.Parameter(torch.Tensor(2 * self.n_embedding))
        self.init_w.data.uniform_(-1,1)        
        self.h_context_embed = nn.Linear(self.n_embedding, self.n_embedding)
        self.v_weight_embed = nn.Linear(2 * self.n_embedding, self.n_embedding)

        # define pointer
        self.high_pointer = Pointer(self.n_embedding, self.n_hidden, self.C)                        
        
    def forward(self, cell_embed, original_node, map, num_cell, costs):

        init_h = None
        h = None
        pos = 0  
        cell_log_prob, cell_reward, cell_action = [], [], []
        for index, item in enumerate(num_cell):
            # get the current cell embedding values
            current_cell_embed = cell_embed[pos: pos + 4 * item, :].clone()
            current_cell = original_node[pos: pos + 4 * item, :].clone()
            current_costs = costs[index].cuda()
            pos = 4 * item
            # calculate query
            h_mean = current_cell_embed.unsqueeze(0).mean(1) # batch * 1 * embedding_size            
            h_bar = self.h_context_embed(h_mean) # batch * 1 * embedding_size
            h_rest = self.v_weight_embed(self.init_w) # 1 * embeddig_size
            query = h_bar + h_rest # batch * embedding_size
            # set the high environment
            self.high_environment = Environment(batch_data = current_cell_embed.unsqueeze(0))    
            
            # define action masking
            high_mask = torch.zeros([1, 4 * item], dtype= torch.int64).cuda()
            # high_mask[:4, :] = 1
            # initialize cell log_prob and cell reward                
            temp_log_prob = []
            temp_reward = []        
            temp_action = []
            for i in range(item):  
                # calculate logits from pointer and get node index
                logits = self.high_pointer(query=query, target=current_cell_embed.unsqueeze(0), mask = high_mask)                 
                node_distribtution = Categorical(logits)
                idx = node_distribtution.sample()
                _cell_log_prob = node_distribtution.log_prob(idx)   
                
                # append action and log probability
                temp_action.append(idx)
                temp_log_prob.append(_cell_log_prob)
                
                # action masking for policy
                I = int(idx / 4) 
                high_mask[:, 4* I: 4* (I + 1)] = 1
                high_mask = self.high_environment.update_state(action_mask= high_mask, next_idx = idx)
                if i > 0:
                    maze = map[index]                    
                    maze = maze.tolist()
                    start_pt = current_cell.gather(0, st_idx.unsqueeze(0).repeat(1,4))
                    end_pt = current_cell.gather(0, idx.unsqueeze(0).repeat(1,4)) 
                    spt = start_pt[:, 2:]
                    ept = end_pt[:, :2]
                    external_reward = torch.norm(ept - spt, dim=1).cuda()                    
                    internal_reward = current_costs[:, st_idx] + current_costs[:, idx]
                    reward = external_reward + internal_reward
                    temp_reward.append(reward)

                st_idx = idx.clone()

                _idx = idx.unsqueeze(1).repeat(1, self.n_embedding) # torch.gather를 사용하기 위해 차원을 맞춰줌
                if init_h is None:
                    init_h = current_cell_embed.gather(1, _idx).squeeze(1)
                h = current_cell_embed.gather(1, _idx).squeeze(1)
                h_rest = self.v_weight_embed(torch.cat([init_h, h], dim = -1)) 
                query = h_bar + h_rest   

            # summation of reward and log_probability
            temp_reward = torch.stack(temp_reward, dim=1).squeeze(2)            
            sum_reward = torch.sum(temp_reward.squeeze(0))     
            
            temp_log_prob = torch.stack(temp_log_prob, dim=1).squeeze(0)
            sum_log_prob = torch.sum(temp_log_prob, dim=0)

            # append the reward,log_probabiltiy and action 
            cell_reward.append(sum_reward)
            cell_log_prob.append(sum_log_prob)
            cell_action.append(temp_action)
        # reshape the reward and log_probability
        """
        Ags:
            cell_reward = [batch_size, 1] --> type is tensor
            cell_log_prob = [batch_size, 1] --> type is tensor
            cell_action = [batch_size, num_cells] --> type is list
        """
        cell_reward = torch.stack(cell_reward, dim=0)
        cell_log_prob = torch.stack(cell_log_prob, dim = 0)

        return cell_log_prob, cell_reward, cell_action                    

# define total model here
class HCPP(torch.nn.Module):
    def __init__(self, 
                n_feature, 
                n_hidden,                 
                n_embedding,                                 
                n_head,
                C):
        super(HCPP, self).__init__()
        self.h_encoder = Encoder(n_feature= n_feature, n_hidden= n_hidden)
        self.h_decoder = Decoder(n_embedding= n_embedding, n_hidden= n_hidden, n_head=n_head, C = C)        

    def forward(self, map, num_cell, points, costs):             
        cell_embed, original_node = self.h_encoder(points, mask = None)                  
        high_log_prob, high_reward, high_action = self.h_decoder(cell_embed, original_node, map, num_cell, costs)
        return high_log_prob, high_reward, high_action


